"""Build local speaker PDFs and attach notes to the exported Gamma PPTX.

No application, Confluence, model, benchmark or corporate network calls.
Dependencies: reportlab for PDFs; python-pptx for the exported presentation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from html import escape
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs/presentation"
OUTPUT = DOCS / "artifacts/2026-10-09"
DECK_BASENAME = "knowledge-reviewer-rag-v3"
BUNDLE_BASENAME = "knowledge-demo-v3"
SLIDES_SCRIPT = DOCS / "2026-10-09-speaker-slides-v3.md"
DEMO_SCRIPT = DOCS / "2026-10-09-demo-reading-script-v3.md"
METRICS = DOCS / "2026-10-09-serious-metrics.md"
FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")


def register_fonts() -> None:
    for name, filename in (
        ("Reading", "DejaVuSans.ttf"),
        ("ReadingBold", "DejaVuSans-Bold.ttf"),
        ("ReadingItalic", "DejaVuSans-Oblique.ttf"),
        ("ReadingMono", "DejaVuSansMono.ttf"),
    ):
        pdfmetrics.registerFont(TTFont(name, str(FONT_DIR / filename)))
    pdfmetrics.registerFontFamily(
        "Reading", normal="Reading", bold="ReadingBold", italic="ReadingItalic",
        boldItalic="ReadingBold",
    )


def styles() -> dict[str, ParagraphStyle]:
    base = dict(fontName="Reading", textColor=colors.HexColor("#202630"),
                alignment=TA_LEFT, splitLongWords=True)
    return {
        "body": ParagraphStyle("Body", fontSize=12.5, leading=19,
                               spaceAfter=9, **base),
        "h1": ParagraphStyle("H1", fontName="ReadingBold", fontSize=21,
                             leading=28, spaceAfter=18, keepWithNext=True,
                             textColor=colors.HexColor("#162F45")),
        "h2": ParagraphStyle("H2", fontName="ReadingBold", fontSize=16,
                             leading=23, spaceBefore=12, spaceAfter=12,
                             keepWithNext=True, textColor=colors.HexColor("#162F45")),
        "h3": ParagraphStyle("H3", fontName="ReadingBold", fontSize=12.5,
                             leading=19, spaceBefore=9, spaceAfter=8,
                             keepWithNext=True, textColor=colors.HexColor("#386078")),
        "action": ParagraphStyle("Action", fontSize=10.5, leading=16,
                                 spaceAfter=7, **{**base, "textColor": colors.HexColor("#566574")}),
        "code": ParagraphStyle("Code", fontName="ReadingMono", fontSize=9,
                               leading=14, spaceAfter=9, backColor=colors.HexColor("#F0F3F6"),
                               borderPadding=8, splitLongWords=True),
        "cell": ParagraphStyle("Cell", fontSize=8.8, leading=13, **base),
    }


def inline(value: str) -> str:
    # Keep labels readable while preserving actual clickable links in the PDF.
    links: list[tuple[str, str]] = []
    def stash(match: re.Match[str]) -> str:
        links.append((match[1], match[2]))
        return f"LINKTOKEN{len(links) - 1}END"
    value = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", stash, value)
    value = escape(value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", value)
    value = re.sub(r"`([^`]+)`", r'<font name="ReadingMono">\1</font>', value)
    for index, (label, target) in enumerate(links):
        label = escape(label)
        if target.startswith(("https://", "http://")):
            replacement = f'<a href="{escape(target, quote=True)}" color="#235B85">{label}</a>'
        else:
            replacement = label
        value = value.replace(f"LINKTOKEN{index}END", replacement)
    return value


def markdown_flowables(text: str, page_sections: bool = False) -> list:
    st = styles()
    lines = text.splitlines()
    result: list = []
    index = 0
    action = False
    seen_numbered_section = False
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line:
            continue
        if line.startswith("```"):
            content = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                content.append(lines[index])
                index += 1
            index += 1
            code = "<br/>".join(escape(item).replace(" ", "&#160;") for item in content)
            result.append(Paragraph(code or " ", st["code"]))
            continue
        heading = re.match(r"^(#{1,3})\s+(.+)$", line)
        if heading:
            level, title = len(heading[1]), heading[2]
            numbered = level == 2 and bool(re.match(r"\d+\.\s", title))
            if page_sections and numbered:
                if seen_numbered_section or result:
                    result.append(PageBreak())
                seen_numbered_section = True
            action = "НЕ ЧИТАТЬ" in title.upper() or "ОСНОВАНИЯ" in title.upper()
            result.append(Paragraph(inline(title), st[f"h{level}"]))
            continue
        if line.startswith("|"):
            rows = []
            while True:
                if not re.fullmatch(r"[|:\-\s]+", line):
                    rows.append([Paragraph(inline(cell.strip()), st["cell"])
                                 for cell in line.strip("|").split("|")])
                if index >= len(lines) or not lines[index].strip().startswith("|"):
                    break
                line = lines[index].strip()
                index += 1
            if rows:
                columns = len(rows[0])
                table = Table(rows, colWidths=[(A4[0] - 92) / columns] * columns,
                              repeatRows=1, hAlign="LEFT")
                table.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF3")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#D8E1E8")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ]))
                result.extend([table, Spacer(1, 12)])
            continue
        if line.startswith("- "):
            result.append(Paragraph("• " + inline(line[2:]), st["action" if action else "body"]))
            continue
        block = [line]
        while index < len(lines) and lines[index].strip():
            candidate = lines[index].strip()
            if candidate.startswith(("#", "```", "|", "- ")):
                break
            block.append(candidate)
            index += 1
        result.append(Paragraph(inline(" ".join(block)), st["action" if action else "body"]))
    return result


def footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("Reading", 8)
    canvas.setFillColor(colors.HexColor("#647282"))
    canvas.drawString(46, 26, "Материалы выступающего · 9 октября 2026 · v3")
    canvas.drawRightString(A4[0] - 46, 26, str(doc.page))
    canvas.restoreState()


def render_pdf(destination: Path, documents: list[str], title: str,
               page_sections: bool = False) -> None:
    story = []
    for index, document in enumerate(documents):
        if index:
            story.append(PageBreak())
        story.extend(markdown_flowables(document, page_sections=page_sections))
    doc = SimpleDocTemplate(str(destination), pagesize=A4, rightMargin=46,
                            leftMargin=46, topMargin=44, bottomMargin=44,
                            title=title, author="Материалы выступающего", pageCompression=1)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def attach_notes(source: Path, destination: Path, script: str) -> dict:
    from pptx import Presentation
    sections = re.split(r"(?m)^## (?=\d+\. )", script)[1:]
    if len(sections) != 7:
        raise ValueError("Expected seven numbered speaker sections")
    presentation = Presentation(str(source))
    if len(presentation.slides) != 7:
        raise ValueError("Expected seven Gamma slides")
    before = [tuple(shape.text for shape in slide.shapes if shape.has_text_frame)
              for slide in presentation.slides]
    for slide, section in zip(presentation.slides, sections):
        title, body = section.split("\n", 1)
        body = re.sub(r"(?m)^### Читать\s*$", "ТЕКСТ ДЛЯ ЧТЕНИЯ", body)
        body = re.sub(r"(?m)^### Не читать\s*$", "ПОДСКАЗКИ — НЕ ЧИТАТЬ ВСЛУХ", body)
        body = body.replace("**", "").replace("`", "")
        frame = slide.notes_slide.notes_text_frame
        if frame is None:
            raise ValueError("Export has no notes body placeholder")
        frame.text = title + "\n" + body.strip()
    presentation.core_properties.title = "Техническая документация: от проверки статьи к поиску ответа"
    presentation.core_properties.subject = "Семь слайдов и полный текст в заметках докладчика"
    presentation.save(str(destination))
    saved = Presentation(str(destination))
    after = [tuple(shape.text for shape in slide.shapes if shape.has_text_frame)
             for slide in saved.slides]
    if before != after:
        raise ValueError("Slide content changed while attaching notes")
    for slide in saved.slides:
        if not slide.notes_slide.notes_text_frame.text.strip():
            raise ValueError("Speaker notes missing")
    with zipfile.ZipFile(destination) as archive:
        invalid = archive.testzip()
        if invalid:
            raise ValueError(f"Invalid PPTX member: {invalid}")
    return {"slide_count": len(saved.slides), "notes_count": len(sections),
            "editable_text_shapes": sum(len(items) for items in after),
            "slide_content_unchanged": True}


def bundle() -> dict:
    artifacts = [
        "README.md", f"{DECK_BASENAME}.pptx", f"{DECK_BASENAME}.pdf",
        "reading-script-v3.pdf", "speaker-slides-v3.pdf", "demo-reading-script-v3.pdf",
        "serious-metrics-v3.pdf",
    ]
    inputs = [(OUTPUT / name, name) for name in artifacts]
    for source in (SLIDES_SCRIPT, DEMO_SCRIPT, METRICS,
                   DOCS / "2026-10-09-slide-outline-v3.md",
                   DOCS / "rag-technical-brief.md", DOCS / "reviewer-technical-brief.md"):
        inputs.append((source, "texts/" + source.name))
    for name in ("reviewer-remote-result.png", "rag-search.png", "rag-code-audit.png"):
        inputs.append((ROOT / "apps/web/public/presentation/assets" / name, "reserve/" + name))
    for source, _ in inputs:
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"Missing regular package input: {source}")
    destination = OUTPUT / f"{BUNDLE_BASENAME}.zip"
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source, name in inputs:
            archive.write(source, BUNDLE_BASENAME + "/" + name)
    with zipfile.ZipFile(destination) as archive:
        invalid = archive.testzip()
        if invalid or len(archive.namelist()) != len(inputs):
            raise ValueError("Invalid presentation package")
    return {"file": str(destination.relative_to(ROOT)), "file_count": len(inputs),
            "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}


def main() -> None:
    global OUTPUT, DECK_BASENAME, BUNDLE_BASENAME
    parser = argparse.ArgumentParser()
    parser.add_argument("--pptx-source", type=Path)
    parser.add_argument("--notes-only", action="store_true")
    parser.add_argument("--bundle-only", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    parser.add_argument("--deck-base", default=DECK_BASENAME)
    parser.add_argument("--bundle-base", default=BUNDLE_BASENAME)
    args = parser.parse_args()
    OUTPUT = args.output_dir.resolve()
    if not OUTPUT.is_relative_to(DOCS / "artifacts"):
        raise ValueError("Output must remain inside docs/presentation/artifacts")
    for value in (args.deck_base, args.bundle_base):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
            raise ValueError("Artifact basename must not contain directory components")
    DECK_BASENAME, BUNDLE_BASENAME = args.deck_base, args.bundle_base
    OUTPUT.mkdir(parents=True, exist_ok=True)
    if args.bundle_only:
        print(json.dumps({"application_or_model_calls": 0, "bundle": bundle()},
                         ensure_ascii=False, indent=2))
        return
    slides_text = SLIDES_SCRIPT.read_text(encoding="utf-8")
    generated = []
    if not args.notes_only:
        register_fonts()
        demo_text = DEMO_SCRIPT.read_text(encoding="utf-8")
        metrics_text = METRICS.read_text(encoding="utf-8")
        for name, contents, title, sections in (
            ("speaker-slides-v3.pdf", [slides_text], "Текст к семи слайдам", True),
            ("demo-reading-script-v3.pdf", [demo_text], "Сценарий демонстрации", True),
            ("reading-script-v3.pdf", [slides_text, demo_text], "Полный текст выступления", True),
            ("serious-metrics-v3.pdf", [metrics_text], "Метрики серьёзной оценки", False),
        ):
            path = OUTPUT / name
            render_pdf(path, contents, title, sections)
            generated.append({"file": str(path.relative_to(ROOT)),
                              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    if args.pptx_source:
        path = OUTPUT / f"{DECK_BASENAME}.pptx"
        details = attach_notes(args.pptx_source, path, slides_text)
        generated.append({"file": str(path.relative_to(ROOT)), **details,
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    print(json.dumps({"application_or_model_calls": 0, "generated": generated},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
