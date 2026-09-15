"""Offline snapshot audit. Reports hashes/counts only; never runs source commands."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

from app.confluence_processing import AC, _fragment, _name, storage_blocks
from app.document_processing import parse_document


def reference_code(root: ET.Element) -> list[str]:
    """Read original code elements independently of the Markdown renderer."""
    result: list[str] = []

    def plain(node: ET.Element) -> str:
        parts = [node.text or ""]
        for child in node:
            parts.append("\n" if _name(child.tag) == "br" else plain(child))
            parts.append(child.tail or "")
        return "".join(parts)

    def visit(node: ET.Element) -> None:
        tag = _name(node.tag)
        if tag in {"script", "style", "noscript", "parameter", "image"}:
            return
        if tag == "structured-macro":
            if node.get(f"{{{AC}}}name") in {"code", "noformat"}:
                body = next(c for c in node if _name(c.tag) == "plain-text-body")
                result.append("".join(body.itertext()))
            else:
                for child in node:
                    if _name(child.tag) == "rich-text-body":
                        visit(child)
            return
        if tag in {"pre", "code"}:
            value = plain(node)
            if tag == "pre" or "\n" in value:
                result.append(value)
            return
        for child in node:
            visit(child)

    visit(root)
    return result


def audit_snapshot(root: Path, manifest_path: Path | None = None) -> dict:
    root = root.resolve()
    manifest_file = manifest_path or root / "latest.json"
    manifest_bytes = manifest_file.read_bytes()
    manifest = json.loads(manifest_bytes)
    if not manifest.get("snapshot_complete") or not manifest.get("inventory_complete"):
        raise ValueError("A complete snapshot is required")
    entries = manifest["pages"]
    if not entries or len({p["id"] for p in entries}) != len(entries):
        raise ValueError("Empty or duplicate snapshot inventory")
    totals: Counter = Counter()
    warnings: Counter = Counter()
    failures: list[dict] = []
    pages: list[dict] = []
    for entry in entries:
        try:
            source = (root / entry["path"]).resolve()
            if not source.is_relative_to(root):
                raise ValueError("Unsafe snapshot path")
            raw = source.read_bytes()
            if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                raise ValueError("Snapshot checksum mismatch")
            page = json.loads(raw)
            if str(page["id"]) != entry["id"] or str(page["version"]["number"]) != entry["version"]:
                raise ValueError("Page identity/version mismatch")
            storage = page["body"]["storage"]["value"]
            expected = reference_code(_fragment(storage))
            blocks, diagnostics = storage_blocks(storage, page["title"])
            code = [b.text for b in blocks if b.kind == "code"]
            actual = []
            for block in code:
                match = re.fullmatch(r"(`{3,})[^\n]*\n([\s\S]*)\n\1", block)
                if match is None:
                    raise ValueError("Malformed code fence")
                actual.append(match[2])
            if expected != actual:
                raise ValueError("Code content/order differs from source")
            hashes = [hashlib.sha256(value.encode()).hexdigest() for value in expected]
            if hashes != diagnostics["code_sha256"]:
                raise ValueError("Code provenance mismatch")
            envelope = {"format": "confluence_storage_v1", "id": entry["id"],
                        "version": entry["version"], "title": page["title"], "storage_html": storage}
            chunks = parse_document("audit.confluence.json", json.dumps(envelope).encode())
            if not chunks or any(not any(block in chunk.text for chunk in chunks) for block in code):
                raise ValueError("Code is missing or split between chunks")
            totals.update(pages=1, code_blocks=len(code), chunks=len(chunks),
                          oversized_chunks=sum(c.metadata["oversized_atomic_block"] for c in chunks))
            warnings.update(diagnostics["warnings"])
            pages.append({"page_id": entry["id"], "version": entry["version"],
                          "snapshot_sha256": entry["sha256"], "code_sha256": hashes,
                          "chunks": len(chunks), "warnings": diagnostics["warnings"]})
        except (ValueError, KeyError, TypeError, OSError, StopIteration) as exc:
            # No raw source content or parser exception excerpts in reports.
            failures.append({"page_id": entry["id"], "error_type": type(exc).__name__})
    return {"run_id": manifest["run_id"], "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "expected_pages": len(entries), "totals": dict(totals), "warning_page_counts": dict(warnings),
            "failures": failures, "passed": not failures and totals["pages"] == len(entries), "pages": pages}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit_snapshot(args.snapshot, args.manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        args.output.chmod(0o600)
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({k: v for k, v in report.items() if k != "pages"}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
