from __future__ import annotations

import csv
import json
import re
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from xml.etree.ElementTree import ParseError

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from app.config import settings


class _TextExtractor(HTMLParser):
    BLOCK_TAGS = frozenset({
        "article", "br", "div", "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "p", "section", "table", "td", "th", "tr",
    })

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "template"}:
            self.ignored.append(tag.lower())
        if self.ignored:
            return
        if tag.lower() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.ignored:
            if tag.lower() == self.ignored[-1]:
                self.ignored.pop()
            return
        if tag.lower() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            self.parts.append(data)


def _decode(data: bytes) -> str:
    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return data.decode("utf-32")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _strip_markup(text: str) -> str:
    parser = _TextExtractor()
    parser.feed(text)
    parser.close()
    return "".join(parser.parts)


def _normalize(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    normalized = "\n".join(lines)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def normalize_exact_text(value: str) -> str:
    """Normalize punctuation and case for exact identifier retrieval."""

    return "".join(
        character
        for character in value.casefold().replace("ё", "е")
        if character.isalnum()
    )


def extract_text(filename: str, data: bytes) -> str:
    extension = Path(filename).suffix.lower()
    text = _decode(data)
    if "\x00" in text:
        raise ValueError("Text document contains binary NUL bytes or an unsupported encoding")
    if len(text) > settings.parser_max_text_chars:
        raise ValueError("Text document exceeds the extracted text safety limit")

    if extension == ".json":
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("JSON document is invalid") from exc
    elif extension == ".csv":
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        try:
            rows = csv.reader(StringIO(text), dialect=dialect, strict=True)
            text = "\n".join(" | ".join(cell.strip() for cell in row) for row in rows)
        except csv.Error as exc:
            raise ValueError("CSV document is invalid") from exc
    elif extension == ".xml":
        try:
            root = SafeET.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
            text = "\n".join(part.strip() for part in root.itertext() if part.strip())
        except (ParseError, DefusedXmlException) as exc:
            raise ValueError("XML document is invalid or contains forbidden declarations") from exc
    elif extension in {".html", ".htm"}:
        text = _strip_markup(text)

    return _normalize(text)


def split_into_chunks(text: str) -> list[str]:
    max_chars = settings.chunk_max_chars
    overlap = min(settings.chunk_overlap_chars, max_chars // 2)
    min_chars = settings.chunk_min_chars
    chunks: list[str] = []
    start = 0

    while start < len(text):
        hard_end = min(start + max_chars, len(text))
        end = hard_end

        if hard_end < len(text):
            search_from = start + max_chars // 2
            candidates = [
                text.rfind("\n\n", search_from, hard_end),
                text.rfind("\n", search_from, hard_end),
                text.rfind(". ", search_from, hard_end),
                text.rfind(" ", search_from, hard_end),
            ]
            boundary = max(candidates)
            if boundary > start:
                end = boundary + (2 if text[boundary:boundary + 2] in {"\n\n", ". "} else 1)

        chunk = text[start:end].strip()
        if chunk:
            if len(chunk) < min_chars and chunks:
                chunks[-1] = f"{chunks[-1]}\n\n{chunk}"
            else:
                chunks.append(chunk)

        if end >= len(text):
            break
        start = max(end - overlap, start + 1)

    return chunks
