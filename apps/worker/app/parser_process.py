"""Sandboxed parsing of documents and PDF page ranges with JSON IPC.

Native PDF/image decoders cannot be cancelled safely in an asyncio thread.
The worker runs this module as a short-lived child and exchanges JSON only.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from app.config import settings
from app.document_processing import ParsedChunk, media_chunks, parse_document


def parse_document_isolated(filename: str, data: bytes) -> list[ParsedChunk]:
    if not data:
        raise ValueError("Document is empty")
    if len(data) > settings.parser_max_input_mb * 1024 * 1024:
        raise ValueError("Document exceeds the parser input safety limit")
    output_limit = min(128_000_000, settings.parser_max_text_chars * 32)
    if Path(filename).suffix.lower() != ".pdf":
        payload, _ = _run_child(filename, data, [], settings.parser_timeout_seconds, output_limit)
        return _read_chunks(payload, allow_empty=False)

    size = settings.pdf_batch_pages
    deadline = time.monotonic() + settings.pdf_timeout_seconds
    output: list[ParsedChunk] = []
    start = vision_images = extracted_chars = chunk_chars = 0
    total_pages = None
    while total_pages is None or start < total_pages:
        remaining_time = deadline - time.monotonic()
        if remaining_time <= 0:
            raise ValueError("PDF parsing exceeded its total time limit")
        args = [str(start), str(start + size), str(vision_images),
                str(settings.parser_max_text_chars - extracted_chars)]
        payload, wire_size = _run_child(
            filename, data, args, min(settings.parser_timeout_seconds, remaining_time), output_limit
        )
        output_limit -= wire_size
        pages, calls, chars = (payload.get(k) for k in ("total_pages", "vision_images", "text_chars"))
        if (type(pages) is not int or pages < 0 or total_pages not in (None, pages)
                or type(calls) is not int or not vision_images <= calls <= settings.vision_max_images_per_document
                or type(chars) is not int or chars < 0):
            raise ValueError("PDF parser returned invalid batch metadata")
        total_pages, vision_images = pages, calls
        extracted_chars += chars
        chunks = _read_chunks(payload, allow_empty=True)
        chunk_chars += sum(len(chunk.text) for chunk in chunks)
        if max(chunk_chars, extracted_chars) > settings.parser_max_text_chars:
            raise ValueError("PDF exceeds the extracted text safety limit")
        output.extend(chunks)
        start += size
    if not output:
        raise ValueError("PDF produced no readable text; check the scan or vision configuration")
    return output


def _run_child(
    filename: str, data: bytes, args: list[str], timeout: float, output_limit: int
) -> tuple[dict, int]:
    with subprocess.Popen(
        [sys.executable, "-m", "app.parser_process", filename, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        cwd=Path(__file__).resolve().parent.parent,
    ) as process:
        try:
            stdout, _ = process.communicate(input=data, timeout=timeout)
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
    if len(stdout) > output_limit:
        raise ValueError("Document parser response exceeds the safe output limit")
    try:
        payload = json.loads(stdout)
        if not payload["ok"]:
            raise ValueError(
                str(payload.get("error", "Document parsing failed"))[:4000]
            )
        return payload, len(stdout)
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("Document parser returned an invalid response") from exc


def _read_chunks(payload: dict, *, allow_empty: bool) -> list[ParsedChunk]:
    try:
        records = payload["chunks"]
        if not isinstance(records, list) or (not records and not allow_empty):
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
        filename = sys.argv[1]
        if len(data) > settings.parser_max_input_mb * 1024 * 1024:
            raise ValueError("Document exceeds the parser input safety limit")
        extra = {}
        if len(sys.argv) > 2:
            from app.media_processing import extract_pdf_range

            start, stop, vision_images, char_limit = map(int, sys.argv[2:])
            pages, total_pages, calls = extract_pdf_range(data, start, stop, vision_images, char_limit)
            chunks = media_chunks(filename, pages, "pdf_cpu_v1")
            extra = {"total_pages": total_pages, "vision_images": calls, "text_chars": sum(len(p.text) for p in pages)}
        else:
            chunks = parse_document(filename, data)
        payload = {
            "ok": True, **extra,
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
