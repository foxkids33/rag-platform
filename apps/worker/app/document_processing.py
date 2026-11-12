from __future__ import annotations

import re
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from app.config import settings
from app.text_processing import _decode, extract_text, split_into_chunks

TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".json", ".html", ".htm", ".xml"}
WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


@dataclass(frozen=True, slots=True)
class ParsedChunk:
    text: str
    search_text: str
    parent_text: str
    parent_chunk_id: uuid.UUID
    heading: str | None
    heading_breadcrumb: str | None
    page_start: int | None
    page_end: int | None
    metadata: dict[str, Any]


def parse_document(filename: str, data: bytes) -> list[ParsedChunk]:
    if not data:
        raise ValueError("Document is empty")
    if len(data) > settings.parser_max_input_mb * 1024 * 1024:
        raise ValueError("Document exceeds the parser input safety limit")
    if filename.endswith(".confluence.json"):
        chunks = _parse_confluence(data)
    else:
        extension = Path(filename).suffix.lower()
        if extension in {".md", ".markdown"}:
            chunks = _parse_markdown(filename, data)
        elif extension == ".docx":
            chunks = _parse_docx(filename, data)
        elif extension == ".pdf":
            from app.media_processing import extract_pdf

            chunks = _media_chunks(filename, extract_pdf(data), "pdf_cpu_v1")
        elif extension in {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}:
            from app.media_processing import extract_image

            chunks = _media_chunks(filename, extract_image(filename, data), "image_ocr_vision_v1")
        elif extension in TEXT_EXTENSIONS:
            chunks = _parse_plain_text(filename, data)
        else:
            raise ValueError("Unsupported document format")
    if not chunks:
        raise ValueError("Document produced no readable text")
    if sum(len(chunk.text) for chunk in chunks) > settings.parser_max_text_chars:
        raise ValueError("Document exceeds the extracted text safety limit")
    return chunks


def _media_chunks(filename: str, pages: Iterable[Any], parser: str) -> list[ParsedChunk]:
    records = []
    for page in pages:
        heading = f"{filename} — страница {page.page_number}"
        for child in split_into_chunks(page.text):
            records.append({
                "text": child, "search_text": f"{heading}\n\n{child}",
                "heading": heading, "heading_breadcrumb": heading,
                "page_start": page.page_number, "page_end": page.page_number,
                "metadata": {
                    "parser": parser,
                    "format": Path(filename).suffix.lower().lstrip("."),
                    **page.metadata,
                },
            })
    return _build_parent_groups(records)


def _parse_markdown(filename: str, data: bytes) -> list[ParsedChunk]:
    """Normalize prose as before, preserving whole fenced code blocks verbatim.

    Only line endings are normalized inside fences. Even an oversized or
    unclosed fence stays in one child chunk; the latter is explicitly flagged.
    """
    source = _decode(data).replace("\r\n", "\n").replace("\r", "\n")
    fence_pattern = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
    blocks: list[tuple[str, bool, bool]] = []
    prose: list[str] = []
    code: list[str] = []
    opening: str | None = None

    for line in source.splitlines(keepends=True):
        match = fence_pattern.fullmatch(line.removesuffix("\n"))
        if opening is None:
            if match and not (match[1][0] == "`" and "`" in match[2]):
                if prose:
                    blocks.append(("".join(prose), False, True))
                    prose.clear()
                opening = match[1]
                code.append(line)
            else:
                prose.append(line)
        else:
            code.append(line)
            if (
                match
                and match[1][0] == opening[0]
                and len(match[1]) >= len(opening)
                and not match[2].strip()
            ):
                blocks.append(("".join(code), True, True))
                code.clear()
                opening = None

    if code:
        blocks.append(("".join(code), True, False))
    if prose:
        blocks.append(("".join(prose), False, True))
    if not any(is_code for _, is_code, _ in blocks):
        return _parse_plain_text(filename, data)

    records: list[dict[str, Any]] = []
    for block, is_code, closed in blocks:
        # Fences never pass through the whitespace-normalizing text reader or
        # character splitter. Prose keeps the existing extraction behavior.
        children = [block] if is_code else split_into_chunks(extract_text(filename, block.encode("utf-8")))
        for child in children:
            records.append({
                "text": child,
                "search_text": child,
                "heading": None,
                "heading_breadcrumb": None,
                "page_start": None,
                "page_end": None,
                "metadata": {
                    "parser": "markdown_fenced_v1",
                    "format": Path(filename).suffix.lower().lstrip("."),
                    "block_kind": "code" if is_code else "prose",
                    "oversized_atomic_block": is_code and len(child) > settings.chunk_max_chars,
                    "warnings": ["unclosed_code_fence"] if is_code and not closed else [],
                },
            })
    return _build_parent_groups(records)


def _parse_confluence(data: bytes) -> list[ParsedChunk]:
    from app.confluence_processing import read_envelope, storage_blocks

    page = read_envelope(data)
    blocks, diagnostics = storage_blocks(
        page["storage_html"], page["title"], page.get("ancestors", [])
    )
    records: list[dict[str, Any]] = []
    group: list[str] = []
    heading: str | None = None
    breadcrumb: str | None = None

    def flush() -> None:
        if not group:
            return
        value = "\n\n".join(group)
        records.append({
            "text": value,
            "search_text": f"{breadcrumb or page['title']}\n\n{value}",
            "heading": heading,
            "heading_breadcrumb": breadcrumb,
            "page_start": None,
            "page_end": None,
            "metadata": {
                "parser": "confluence_storage_v1",
                "source_page_id": page.get("id"),
                "source_version": page.get("version"),
                "source_url": page.get("source_url"),
                "source_space": page.get("space_key"),
                "labels": page.get("labels", []),
                "oversized_atomic_block": len(value) > settings.chunk_max_chars,
                **diagnostics,
            },
        })
        group.clear()

    for block in blocks:
        length = sum(len(item) + 2 for item in group) + len(block.text)
        if group and (block.breadcrumb != breadcrumb or length > settings.chunk_max_chars):
            flush()
        heading, breadcrumb = block.heading, block.breadcrumb
        group.append(block.text)
    flush()
    return _build_parent_groups(records)


def _parse_docx(filename: str, data: bytes) -> list[ParsedChunk]:
    """Parse DOCX deterministically without initializing OCR/Torch models.

    DOCX is an OpenXML archive, so text, headings and tables can be extracted
    directly. This keeps office documents lightweight and avoids Docling's PDF
    vision pipeline (and its native compiler requirement) for a non-visual
    format.
    """

    try:
        with ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 2000 or sum(item.file_size for item in entries) > 100 * 1024 * 1024:
                raise ValueError("DOCX archive exceeds the safe expansion limit")
            if any(item.flag_bits & 1 for item in entries):
                raise ValueError("Encrypted DOCX archives are not supported")
            info = archive.getinfo("word/document.xml")
            if info.file_size > 10 * 1024 * 1024:
                raise ValueError("DOCX document.xml exceeds the safe parsing limit")
            source = archive.read(info)
    except (BadZipFile, KeyError, OSError) as exc:
        raise ValueError("DOCX archive is invalid or missing word/document.xml") from exc

    if b"<!DOCTYPE" in source.upper() or b"<!ENTITY" in source.upper():
        raise ValueError("DOCX XML contains a forbidden declaration")
    try:
        root = SafeET.fromstring(source, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise ValueError("DOCX document.xml is invalid or contains a forbidden declaration") from exc

    body = root.find(f"{WORD_NAMESPACE}body")
    if body is None:
        return []

    heading_stack: list[str] = []
    blocks: list[tuple[str, list[str], str]] = []
    for element in body:
        kind = element.tag.rsplit("}", 1)[-1]
        if kind == "p":
            text = _docx_text(element)
            if not text:
                continue
            level = _docx_heading_level(element)
            if level is not None:
                heading_stack = heading_stack[: max(0, level - 1)]
                while len(heading_stack) < level - 1:
                    heading_stack.append("")
                heading_stack.append(text)
                continue
            blocks.append((text, [item for item in heading_stack if item], "paragraph"))
        elif kind == "tbl":
            rows: list[str] = []
            for row in element.findall(f"{WORD_NAMESPACE}tr"):
                cells = [
                    _docx_text(cell)
                    for cell in row.findall(f"{WORD_NAMESPACE}tc")
                ]
                cells = [cell for cell in cells if cell]
                if cells:
                    rows.append(" | ".join(cells))
            if rows:
                blocks.append(("\n".join(rows), [item for item in heading_stack if item], "table"))

    records: list[dict[str, Any]] = []
    group: list[str] = []
    group_headings: list[str] = []
    group_kinds: set[str] = set()

    def flush() -> None:
        if not group:
            return
        text = "\n\n".join(group)
        breadcrumb = " > ".join(group_headings) if group_headings else None
        records.append(
            {
                "text": text,
                "search_text": f"{breadcrumb}\n\n{text}" if breadcrumb else text,
                "heading": group_headings[-1] if group_headings else None,
                "heading_breadcrumb": breadcrumb,
                "page_start": None,
                "page_end": None,
                "metadata": {
                    "parser": "docx_xml_v1",
                    "format": Path(filename).suffix.lower().lstrip("."),
                    "headings": list(group_headings),
                    "block_kinds": sorted(group_kinds),
                },
            }
        )
        group.clear()
        group_kinds.clear()

    for text, headings, block_kind in blocks:
        projected = sum(len(item) + 2 for item in group) + len(text)
        if group and (headings != group_headings or projected > settings.chunk_max_chars):
            flush()
        group_headings = headings
        group.append(text)
        group_kinds.add(block_kind)
    flush()
    return _build_parent_groups(records)


def _docx_text(element: ET.Element) -> str:
    parts: list[str] = []
    for child in element.iter():
        name = child.tag.rsplit("}", 1)[-1]
        if name == "t" and child.text:
            parts.append(child.text)
        elif name == "tab":
            parts.append("\t")
        elif name in {"br", "cr"}:
            parts.append("\n")
    return _clean_text("".join(parts))


def _docx_heading_level(paragraph: ET.Element) -> int | None:
    properties = paragraph.find(f"{WORD_NAMESPACE}pPr")
    style = properties.find(f"{WORD_NAMESPACE}pStyle") if properties is not None else None
    if style is None:
        return None
    value = style.get(f"{WORD_NAMESPACE}val", "")
    match = re.search(r"(?:heading|заголовок)[ _-]?(\d+)$", value, flags=re.IGNORECASE)
    if not match:
        return None
    return max(1, min(9, int(match.group(1))))


def _parse_plain_text(filename: str, data: bytes) -> list[ParsedChunk]:
    text = extract_text(filename, data)
    if not text:
        return []

    children = split_into_chunks(text)
    records = [
        {
            "text": child,
            "search_text": child,
            "heading": None,
            "heading_breadcrumb": None,
            "page_start": None,
            "page_end": None,
            "metadata": {
                "parser": "plain_text_v2",
                "format": Path(filename).suffix.lower().lstrip("."),
            },
        }
        for child in children
    ]
    return _build_parent_groups(records)


def _parse_with_docling(filename: str, data: bytes) -> list[ParsedChunk]:
    # Imports are intentionally lazy: the worker can still start and process
    # lightweight text formats even when Docling model initialization fails.
    try:
        from docling.chunking import HybridChunker
        from docling.datamodel.base_models import DocumentStream
        from docling.document_converter import DocumentConverter
        from docling_core.transforms.chunker.tokenizer.huggingface import (
            HuggingFaceTokenizer,
        )
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "PDF/DOCX parsing is not installed in the lightweight worker; "
            "start the Docling profile with `make dev-docling`"
        ) from exc

    stream = DocumentStream(name=filename, stream=BytesIO(data))
    document = DocumentConverter().convert(source=stream).document
    tokenizer = HuggingFaceTokenizer.from_pretrained(
        model_name=settings.chunk_tokenizer_model,
        max_tokens=settings.docling_chunk_max_tokens,
    )
    chunker = HybridChunker(
        tokenizer=tokenizer,
        merge_peers=True,
        repeat_table_header=True,
    )

    records: list[dict[str, Any]] = []
    for chunk in chunker.chunk(dl_doc=document):
        text = _clean_text(getattr(chunk, "text", ""))
        if not text:
            continue

        meta = getattr(chunk, "meta", None)
        headings = _extract_headings(meta)
        page_start, page_end = _extract_pages(meta)
        contextualized = _clean_text(chunker.contextualize(chunk=chunk)) or text

        records.append(
            {
                "text": text,
                "search_text": contextualized,
                "heading": headings[-1] if headings else None,
                "heading_breadcrumb": " > ".join(headings) if headings else None,
                "page_start": page_start,
                "page_end": page_end,
                "metadata": {
                    "parser": "docling_hybrid_v1",
                    "format": Path(filename).suffix.lower().lstrip("."),
                    "headings": headings,
                },
            }
        )

    return _build_parent_groups(records)


def _build_parent_groups(records: Iterable[dict[str, Any]]) -> list[ParsedChunk]:
    """Attach section-level parents to sequential child chunks.

    Chunks with the same heading breadcrumb share a parent until the configured
    parent size is reached. This keeps retrieval focused on small children while
    preserving a larger section context for answer generation.
    """

    source = list(records)
    if not source:
        return []

    output: list[ParsedChunk] = []
    index = 0
    while index < len(source):
        breadcrumb = source[index].get("heading_breadcrumb")
        group: list[dict[str, Any]] = []
        group_chars = 0

        while index < len(source):
            candidate = source[index]
            candidate_breadcrumb = candidate.get("heading_breadcrumb")
            candidate_text = str(candidate["text"])
            added_chars = len(candidate_text) + (2 if group else 0)

            if group and candidate_breadcrumb != breadcrumb:
                break
            if group and group_chars + added_chars > settings.parent_max_chars:
                break

            group.append(candidate)
            group_chars += added_chars
            index += 1

        parent_text = "\n\n".join(str(item["text"]) for item in group).strip()
        parent_id = uuid.uuid4()
        parent_pages = [
            page
            for item in group
            for page in (item.get("page_start"), item.get("page_end"))
            if isinstance(page, int)
        ]
        parent_page_start = min(parent_pages) if parent_pages else None
        parent_page_end = max(parent_pages) if parent_pages else None

        for item in group:
            metadata = dict(item.get("metadata") or {})
            metadata.update(
                {
                    "chunking": "parent_child_v1",
                    "parent_page_start": parent_page_start,
                    "parent_page_end": parent_page_end,
                }
            )
            output.append(
                ParsedChunk(
                    text=str(item["text"]),
                    search_text=str(item.get("search_text") or item["text"]),
                    parent_text=parent_text,
                    parent_chunk_id=parent_id,
                    heading=item.get("heading"),
                    heading_breadcrumb=item.get("heading_breadcrumb"),
                    page_start=item.get("page_start"),
                    page_end=item.get("page_end"),
                    metadata=metadata,
                )
            )

    return output


def _extract_headings(meta: Any) -> list[str]:
    values = getattr(meta, "headings", None) if meta is not None else None
    if not values:
        return []
    return [text for value in values if (text := _clean_text(str(value)))]


def _extract_pages(meta: Any) -> tuple[int | None, int | None]:
    if meta is None:
        return None, None

    pages: list[int] = []
    origin = getattr(meta, "origin", None)
    origin_page = getattr(origin, "page_no", None)
    if isinstance(origin_page, int):
        pages.append(origin_page)

    for item in getattr(meta, "doc_items", None) or []:
        for provenance in getattr(item, "prov", None) or []:
            page_no = getattr(provenance, "page_no", None)
            if isinstance(page_no, int):
                pages.append(page_no)

    return (min(pages), max(pages)) if pages else (None, None)


def _clean_text(value: str) -> str:
    return "\n".join(line.rstrip() for line in value.replace("\x00", "").splitlines()).strip()
