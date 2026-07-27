from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable

from app.config import settings
from app.text_processing import extract_text, split_into_chunks

DOCLING_EXTENSIONS = {".pdf", ".docx"}


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
    extension = Path(filename).suffix.lower()
    if extension in DOCLING_EXTENSIONS:
        return _parse_with_docling(filename, data)
    return _parse_plain_text(filename, data)


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
    from docling.chunking import HybridChunker
    from docling.datamodel.base_models import DocumentStream
    from docling.document_converter import DocumentConverter
    from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer

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
