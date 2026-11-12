"""Per-document parser process: bounded resources, no untrusted pickle IPC.

Native PDF/image decoders cannot be cancelled safely in an asyncio thread.
The worker runs this module as a short-lived child and exchanges JSON only.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

from app.config import settings
from app.document_processing import ParsedChunk, parse_document


def parse_document_isolated(filename: str, data: bytes) -> list[ParsedChunk]:
    with subprocess.Popen(
        [sys.executable, "-m", "app.parser_process", filename],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        cwd=Path(__file__).resolve().parent.parent,
    ) as process:
        try:
            stdout, _ = process.communicate(
                input=data, timeout=settings.parser_timeout_seconds
            )
        except subprocess.TimeoutExpired as exc:
            # Kill the whole group, including a currently running OCR child.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise ValueError(
                "Document parsing exceeded its time limit; split the document"
            ) from exc
    if process.returncode != 0:
        raise ValueError(
            "Document parser exceeded its resource limit or stopped unexpectedly"
        )
    # Text and parent text are duplicated per child for retrieval. Bound the
    # transport as well; never deserialize pickle sent by a native decoder.
    output_limit = min(128_000_000, settings.parser_max_text_chars * 32)
    if len(stdout) > output_limit:
        raise ValueError("Document parser response exceeds the safe output limit")
    try:
        payload = json.loads(stdout)
        if not payload["ok"]:
            raise ValueError(
                str(payload.get("error", "Document parsing failed"))[:4000]
            )
        records = payload["chunks"]
        if not isinstance(records, list) or not records:
            raise ValueError("Document produced no readable text")
        output = []
        for record in records:
            record["parent_chunk_id"] = uuid.UUID(record["parent_chunk_id"])
            output.append(ParsedChunk(**record))
        return output
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("Document parser returned an invalid response") from exc


def _main() -> None:
    # Linux worker container: inherited limits also apply to Tesseract children.
    import resource

    memory_bytes = settings.parser_memory_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    resource.setrlimit(
        resource.RLIMIT_CPU, (settings.parser_cpu_seconds, settings.parser_cpu_seconds)
    )
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    data = sys.stdin.buffer.read(settings.parser_max_input_mb * 1024 * 1024 + 1)
    try:
        chunks = parse_document(sys.argv[1], data)
        payload = {
            "ok": True,
            "chunks": [
                {**asdict(chunk), "parent_chunk_id": str(chunk.parent_chunk_id)}
                for chunk in chunks
            ],
        }
    except ValueError as exc:
        payload = {"ok": False, "error": str(exc)[:4000]}
    except Exception as exc:  # noqa: BLE001 - final child-process error boundary
        # Do not forward decoder/HTTP payloads or environment values to the API.
        payload = {
            "ok": False,
            "error": f"Document parsing failed ({type(exc).__name__})",
        }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    _main()
