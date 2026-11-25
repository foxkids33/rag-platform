"""Structure-preserving Confluence storage reader; never executes embedded content.

Code is kept verbatim (including whitespace) and never split across chunks.
Unknown/visual macros are reported, not silently treated as extracted knowledge.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from html import entities
from xml.etree import ElementTree as ET

AC = "http://atlassian.com/content"
RI = "http://atlassian.com/resource/identifier"


@dataclass(frozen=True)
class ConfluenceBlock:
    text: str
    kind: str = "prose"
    heading: str | None = None
    breadcrumb: str | None = None


def _name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _fenced(value: str, language: str = "") -> str:
    fence = "`" * max(3, 1 + max((len(m[0]) for m in re.finditer(r"`+", value)), default=0))
    language = re.sub(r"[^A-Za-z0-9_+-]", "", language)[:32]
    # The one separator newline before the closing fence is not part of the code.
    return f"{fence}{language}\n{value}\n{fence}"


def _fragment(value: str) -> ET.Element:
    pieces = re.split(r"(<!\[CDATA\[[\s\S]*?\]\]>)", value)
    # Declarations quoted in CDATA are source code, not XML instructions.
    if any(
        re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", part, re.IGNORECASE)
        for part in pieces if not part.startswith("<![CDATA[")
    ):
        raise ValueError("Confluence storage contains a forbidden XML declaration")

    def entity(match: re.Match[str]) -> str:
        name = match[1]
        if name in {"amp", "lt", "gt", "quot", "apos"}:
            return match[0]
        number = entities.name2codepoint.get(name)
        return f"&#{number};" if number else f"&amp;{name};"

    # Do not rewrite entities or whitespace inside CDATA code/configuration.
    value = "".join(
        part if part.startswith("<![CDATA[") else re.sub(r"&([A-Za-z][A-Za-z0-9]+);", entity, part)
        for part in pieces
    )
    try:
        return ET.fromstring(f'<root xmlns:ac="{AC}" xmlns:ri="{RI}">{value}</root>')
    except ET.ParseError as exc:
        raise ValueError("Invalid Confluence storage XML; refusing lossy HTML fallback") from exc


def _code_text(node: ET.Element) -> str:
    """Flatten HTML formatting once; nested code must not create new fences."""
    if _name(node.tag) == "br":
        return "\n"
    return (node.text or "") + "".join(
        _code_text(child) + (child.tail or "") for child in node
    )


def storage_blocks(storage: str, title: str, ancestors: list[str] | None = None) -> tuple[list[ConfluenceBlock], dict]:
    root = _fragment(storage)
    warnings: set[str] = set()
    code_hashes: list[str] = []
    macros: dict[str, int] = {}
    code_blocks: list[str] = []

    def emit_code(value: str, language: str = "") -> str:
        code_hashes.append(hashlib.sha256(value.encode()).hexdigest())
        code_blocks.append(_fenced(value, language))
        # NUL cannot occur in valid input XML. Structural placeholders prevent
        # surrounding HTML (headings/tables/inline styles) from corrupting fences.
        return f"\n\n\x00RAG-CODE:{len(code_blocks) - 1}\x00\n\n"

    def children(node: ET.Element) -> str:
        return (node.text or "") + "".join(render(child) + (child.tail or "") for child in node)

    def render(node: ET.Element) -> str:
        tag = _name(node.tag)
        if tag in {"script", "style", "noscript", "parameter"}:
            return ""
        if tag == "structured-macro":
            name = node.get(f"{{{AC}}}name", "unknown")
            macros[name] = macros.get(name, 0) + 1
            params = {
                item.get(f"{{{AC}}}name", ""): "".join(item.itertext())
                for item in node if _name(item.tag) == "parameter"
            }
            if name in {"code", "noformat"}:
                body = next((n for n in node if _name(n.tag) == "plain-text-body"), None)
                if body is None:
                    raise ValueError("Confluence code macro has no plain-text body")
                code = "".join(body.itertext())
                caption = params.get("title", "")
                return "\n\n" + (caption + "\n\n" if caption else "") + emit_code(code, params.get("language", ""))
            body = next((n for n in node if _name(n.tag) == "rich-text-body"), None)
            if body is not None:
                label = params.get("title", "")
                if name in {"warning", "note", "info", "tip"}:
                    label = f"{name.upper()}: {label}".rstrip()
                return "\n\n" + (label + "\n" if label else "") + children(body) + "\n\n"
            if name in {"toc", "children", "contentbylabel", "recently-updated", "listlabels"}:
                return ""
            # Plugin parameters can contain large style JSON or raw HTML/JS.
            warnings.add(f"unresolved_macro:{name}")
            return f"\n\n[Макрос {name}: содержимое не извлечено]\n\n"
        if tag == "image":
            attachment = next((n for n in node.iter() if _name(n.tag) == "attachment"), None)
            filename = attachment.get(f"{{{RI}}}filename", "") if attachment is not None else ""
            warnings.add("images_not_recognized")
            return f"\n[Изображение: {filename}; распознавание отложено]\n"
        if tag == "attachment":
            return node.get(f"{{{RI}}}filename", "")
        if tag == "page":
            return node.get(f"{{{RI}}}content-title", "")
        if tag == "pre":
            value = _code_text(node)
            return emit_code(value)
        if tag == "code":
            content = _code_text(node)
            if not content:
                return ""
            if "\n" in content:
                return emit_code(content)
            fence = "`" * max(1, 1 + max((len(m[0]) for m in re.finditer(r"`+", content)), default=0))
            return f"{fence}{content}{fence}"
        content = children(node)
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            return f"\n\n{'#' * int(tag[1])} {content.strip()}\n\n"
        if tag in {"ol", "ul"}:
            # children were already rendered above: avoid rendering code twice.
            return "\n\n" + content + "\n\n"
        if tag == "li":
            # Ordering is retained. Explicit numbering is assigned in a pre-pass.
            marker = node.get("data-rag-marker", "-")
            if content.lstrip().startswith("\x00RAG-CODE:"):
                return f"\n{marker}\n\n{content.strip()}\n\n"
            return f"\n{marker} {content.strip()}\n"
        if tag == "a":
            url = node.get("href", "")
            if url.startswith(("https://", "http://", "/")):
                return f"{content} ({url})" if content.strip() != url else content
            return content
        if tag in {"td", "th"}:
            if "\x00RAG-CODE:" in content:
                return "\n\n" + content.strip() + "\n\n"
            return content.strip() + " | "
        if tag == "tr":
            if "\x00RAG-CODE:" in content:
                return "\n\n[Строка таблицы]\n\n" + content + "\n\n"
            return "\n| " + content.strip() + "\n"
        if tag in {"p", "div", "table", "layout-cell", "layout-section", "task"}:
            return "\n\n" + content + "\n\n"
        if tag == "br":
            return "\n"
        return content

    for listing in root.iter():
        if _name(listing.tag) != "ol":
            continue
        try:
            number = int(listing.get("start", "1"))
        except ValueError:
            number = 1
        for item in listing:
            if _name(item.tag) == "li":
                item.set("data-rag-marker", f"{number}.")
                number += 1

    rendered = children(root)
    blocks: list[ConfluenceBlock] = []
    headings: list[tuple[int, str]] = []
    breadcrumb_base = [*(ancestors or []), title]
    # Resolve typed code placeholders only after rendering surrounding markup.
    pieces: list[tuple[str, bool]] = []
    cursor = 0
    for match in re.finditer(r"\x00RAG-CODE:(\d+)\x00", rendered):
        pieces.extend([(rendered[cursor:match.start()], False), (code_blocks[int(match[1])], True)])
        cursor = match.end()
    pieces.append((rendered[cursor:], False))
    for piece, is_code in pieces:
        if is_code:
            values = [(piece, "code")]
        else:
            values = [(part.strip(), "prose") for part in re.split(r"\n\s*\n", piece) if part.strip()]
        for value, kind in values:
            if kind == "prose":
                value = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines())
                match = re.match(r"^(#{1,6}) (.+)$", value)
                if match:
                    depth = len(match[1])
                    while headings and headings[-1][0] >= depth:
                        headings.pop()
                    headings.append((depth, match[2]))
            labels = [label for _, label in headings]
            blocks.append(ConfluenceBlock(value, kind, labels[-1] if labels else title,
                                          " > ".join(breadcrumb_base + labels)))
    if not blocks:
        blocks.append(ConfluenceBlock(title, heading=title, breadcrumb=" > ".join(breadcrumb_base)))
    return blocks, {"warnings": sorted(warnings), "macros": macros, "code_sha256": code_hashes}


def read_envelope(data: bytes) -> dict:
    page = json.loads(data)
    if not isinstance(page, dict) or page.get("format") != "confluence_storage_v1":
        raise ValueError("Invalid Confluence page envelope")
    if not isinstance(page.get("storage_html"), str) or not isinstance(page.get("title"), str):
        raise ValueError("Confluence page envelope is missing storage_html/title")
    return page
