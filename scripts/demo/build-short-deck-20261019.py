#!/usr/bin/env python3
"""Local, editable six-slide demo deck. No external presentation service is used.

Example (the cached environment is sufficient):
  UV_CACHE_DIR=/tmp/rag-presentation-v3-uv-cache uv run --offline --no-project \
    --python /usr/bin/python3 --with python-pptx --with reportlab python \
    scripts/demo/build-short-deck-20261019.py --screens-dir ... \
    --content-json ... --output-dir ...

Screenshots must be genuine image files. Final builds reject missing screenshots.
Only --allow-missing permits conspicuous draft placeholders. All surrounding text,
cards and diagrams are editable PowerPoint objects; screenshots remain images.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
from xml.sax.saxutils import escape

from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE, MSO_CONNECTOR
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak


ROOT = Path(__file__).resolve().parents[2]
W, H = 13.333333, 7.5
FONT = "DejaVu Sans"
INK, MUTED, ACCENT = "182235", "64738D", "5364FF"
LIGHT, BORDER, WHITE = "F5F7FC", "DDE3EE", "FFFFFF"
DEFAULT = {
    "title": "Документация, которой можно пользоваться",
    "date": "19.10.2026",
    "slides": [
        {"title": "Хорошая статья — только половина решения",
         "subtitle": "Автору нужно подготовить инструкцию. Инженеру — быстро найти подходящие действия.",
         "notes": "Сначала обозначим проблему: качество инструкции и доступность нужного знания — разные задачи."},
        {"title": "RAG: ответ вместе с источниками", "screenshot": "rag-answer.png",
         "caption": "Снимок стенда 19.10.2026. Сохранённый ответ.",
         "bullets": [{"title": "Одна рабочая область", "text": "Документы, база знаний и диалог в одном контексте."},
                     {"title": "Гибридный поиск", "text": "Смысловой и текстовый поиск дополняют друг друга."},
                     {"title": "Проверяемый ответ", "text": "Открываем документ и сверяем исходный фрагмент."}],
         "notes": "Показываем реальный сохранённый ответ и переход к источнику."},
        {"title": "RAG: важна вся последовательность действий", "screenshot": "rag-commands.png",
         "caption": "Снимок стенда 19.10.2026. Сохранённый ответ.",
         "bullets": [{"title": "Не отдельная строка", "text": "Команды и окружающие условия рассматриваются вместе."},
                     {"title": "Структура документа", "text": "Заголовки, код и разделы сохраняют контекст поиска."},
                     {"title": "Сверка с оригиналом", "text": "Источник нужен до выполнения действий в системе."}],
         "notes": "Полезный ответ инженеру — это не случайная команда, а подходящая последовательность с условиями."},
        {"title": "Reviewer: проверка ещё до публикации", "screenshot": "reviewer-analysis.png",
         "caption": "Снимок локального стенда Confluence 19.10.2026.",
         "bullets": [{"title": "Внутри Confluence", "text": "Автор работает с текущей статьёй, а не копирует её в отдельный чат."},
                     {"title": "Требования к статье", "text": "Проверяем полноту troubleshooting-инструкции и команды."},
                     {"title": "Замечание по месту", "text": "Диагноз связан с конкретным фрагментом документа."}],
         "notes": "Теперь вторая сторона процесса: готовим сам источник знаний, прежде чем искать по нему."},
        {"title": "Reviewer: решение о правке остаётся у автора", "screenshot": "reviewer-edit.png",
         "caption": "Снимок локального стенда Confluence 19.10.2026.",
         "bullets": [{"title": "Точечная замена", "text": "Видно, какой фрагмент предлагается изменить и почему."},
                     {"title": "Контроль автора", "text": "Правку можно принять; публикация не выполняется автоматически."},
                     {"title": "Не автопилот", "text": "Рекомендации модели требуют проверки человеком."}],
         "notes": "На этом экране важна не магия генерации, а управляемость точечной правки."},
        {"title": "Два приложения — один цикл знаний",
         "subtitle": "Качество при создании. Доступность при использовании.",
         "notes": "Reviewer помогает подготовить статью; Confluence хранит опубликованный источник; RAG помогает найти ответ. Между приложениями — общий источник знаний, а не волшебная автоматическая интеграция."}
    ]
}


def rgb(value):
    return RGBColor.from_string(value)


def box(slide, x, y, w, h, fill=LIGHT, line=None, rounded=True):
    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE if rounded else MSO_SHAPE.RECTANGLE,
                                   Inches(x), Inches(y), Inches(w), Inches(h))
    if rounded:
        shape.adjustments[0] = 0.09
    shape.fill.solid()
    shape.fill.fore_color.rgb = rgb(fill)
    if line:
        shape.line.color.rgb = rgb(line)
        shape.line.width = Pt(0.8)
    else:
        shape.line.fill.background()
    return shape


def txt(slide, x, y, w, h, value, size=18, color=INK, bold=False,
        align=PP_ALIGN.LEFT, valign=MSO_ANCHOR.TOP):
    shape = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    frame = shape.text_frame
    frame.clear()
    frame.margin_left = frame.margin_right = 0
    frame.margin_top = frame.margin_bottom = 0
    frame.word_wrap = True
    frame.vertical_anchor = valign
    for i, line in enumerate(str(value).split("\n")):
        p = frame.paragraphs[0] if i == 0 else frame.add_paragraph()
        p.text = line
        p.alignment = align
        p.space_before = Pt(0)
        p.space_after = Pt(0)
        p.line_spacing = 1.1
        for run in p.runs:
            run.font.name = FONT
            run.font.size = Pt(size)
            run.font.bold = bold
            run.font.color.rgb = rgb(color)
    return shape


def line(slide, x1, y1, x2, y2, color=BORDER, width=0.8):
    s = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT,
                                   Inches(x1), Inches(y1), Inches(x2), Inches(y2))
    s.line.color.rgb = rgb(color)
    s.line.width = Pt(width)


def chevron(slide, x, y, w=0.15, h=0.22):
    return box_shape(slide, MSO_SHAPE.CHEVRON, x, y, w, h, ACCENT)


def box_shape(slide, kind, x, y, w, h, fill):
    s = slide.shapes.add_shape(kind, Inches(x), Inches(y), Inches(w), Inches(h))
    s.fill.solid()
    s.fill.fore_color.rgb = rgb(fill)
    s.line.fill.background()
    return s


def base(prs, n, item, total):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = rgb(WHITE)
    txt(slide, 0.65, 0.30, 10.6, 0.22, item.get("eyebrow", "ДОКУМЕНТАЦИЯ  /  RAG + CONFLUENCE AI REVIEWER"),
        size=10, color=ACCENT, bold=True)
    title = item["title"]
    title_size = min(29, 29 * 68 / max(68, len(title)))
    txt(slide, 0.65, 0.79, 12.0, 0.62, title, size=title_size, bold=True)
    if item.get("subtitle"):
        txt(slide, 0.65, 1.43, 12.0, 0.43, item["subtitle"], size=15, color=MUTED)
    line(slide, 0.65, 6.96, 12.68, 6.96)
    txt(slide, 0.65, 7.12, 11.0, 0.19,
        item.get("footer", "Локальная демонстрация  /  19 октября 2026"), size=8.5, color=MUTED)
    box(slide, 12.08, 7.13, 0.60, 0.045, ACCENT, rounded=True)
    txt(slide, 12.02, 0.32, 0.65, 0.20, f"{n:02d}", size=10, color=MUTED, align=PP_ALIGN.RIGHT)
    notes = item.get("notes", "")
    if isinstance(notes, list):
        notes = "\n\n".join(notes)
    slide.notes_slide.notes_text_frame.text = notes
    return slide


def bullet_column(slide, bullets):
    if len(bullets) > 3:
        raise ValueError("At most three short points per screenshot slide")
    for i, point in enumerate(bullets):
        y = 1.98 + i * 1.43
        txt(slide, 9.84, y, 0.35, 0.30, f"{i+1:02d}", size=12, color=ACCENT, bold=True)
        txt(slide, 10.27, y - 0.01, 2.40, 0.56, point["title"], size=17, bold=True)
        txt(slide, 9.84, y + 0.58, 2.83, 0.69, point["text"], size=13.5, color=MUTED)
        if i < len(bullets) - 1:
            line(slide, 9.84, y + 1.22, 12.67, y + 1.22)


def screen_path(name, screens_dir):
    p = Path(name)
    return p if p.is_absolute() else screens_dir / p


def screenshot(slide, filename, x, y, w, h, screens_dir, allow_missing):
    p = screen_path(filename, screens_dir)
    box(slide, x, y, w, h, LIGHT, BORDER)
    if not p.is_file():
        if not allow_missing:
            raise FileNotFoundError(f"Required genuine screenshot is missing: {p}")
        txt(slide, x + 0.2, y + h / 2 - 0.35, w - 0.4, 0.7,
            "MISSING SCREENSHOT\n" + p.name, size=20, color="B33A3A", align=PP_ALIGN.CENTER)
        return {"path": str(p), "missing": True}
    with Image.open(p) as im:
        iw, ih = im.size
    pad = 0.07
    scale = min((w - 2 * pad) / iw, (h - 2 * pad) / ih)
    dw, dh = iw * scale, ih * scale
    slide.shapes.add_picture(str(p), Inches(x + (w - dw) / 2), Inches(y + (h - dh) / 2),
                             width=Inches(dw), height=Inches(dh))
    return {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            "width": iw, "height": ih}


def screen_slide(prs, n, item, args):
    slide = base(prs, n, item, 6)
    y = 1.96 if item.get("subtitle") else 1.68
    height = 4.47 if item.get("subtitle") else 4.75
    screens = item.get("screenshots", [item.get("screenshot")])
    if not all(screens) or len(screens) not in (1, 2):
        raise ValueError(f"Slide {n} needs one or two genuine screenshots")
    assets = []
    if len(screens) == 1:
        assets.append(screenshot(slide, screens[0], 0.65, y, 8.76, height,
                                 args.screens_dir, args.allow_missing))
    else:
        labels = item.get("screen_labels", ["До", "После"])
        for i, source in enumerate(screens):
            x = 0.65 + i * 4.47
            txt(slide, x + 0.07, y + 0.03, 4.12, 0.28, labels[i], 13, ACCENT, True)
            assets.append(screenshot(slide, source, x, y + 0.39, 4.29, height - 0.39,
                                     args.screens_dir, args.allow_missing))
    txt(slide, 0.69, 6.56, 8.65, 0.23, item.get("caption", ""), size=9, color=MUTED)
    bullet_column(slide, item.get("bullets", []))
    return assets


def problem_slide(prs, item):
    slide = base(prs, 1, item, 6)
    cards = item.get("cards", [
        {"label": "АВТОР СТАТЬИ", "title": "Подготовить инструкцию",
         "body": "Что проверить?\nКакие действия выполнить?\nКак понять, что проблема решена?",
         "product": "Confluence AI Reviewer", "result": "Помощь до публикации"},
        {"label": "ИНЖЕНЕР", "title": "Найти нужные действия",
         "body": "Какая статья подходит?\nГде полная последовательность?\nМожно ли проверить источник?",
         "product": "RAG-платформа", "result": "Ответ по документам"}
    ])
    for i, card in enumerate(cards):
        x = 0.65 + i * 6.19
        box(slide, x, 2.04, 5.84, 3.61, LIGHT, BORDER)
        txt(slide, x + 0.28, 2.35, 5.28, 0.26, card["label"], 11, ACCENT, True)
        txt(slide, x + 0.28, 2.88, 5.28, 0.46, card["title"], 23, INK, True)
        txt(slide, x + 0.28, 3.56, 5.28, 1.12, card["body"], 17, MUTED)
        line(slide, x + 0.28, 4.82, x + 5.56, 4.82)
        txt(slide, x + 0.28, 5.04, 5.28, 0.34, card["product"], 18, ACCENT, True)
    txt(slide, 0.65, 6.05, 12.03, 0.50,
        item.get("takeaway", "Не просто хранить статьи — помогать создавать и применять знания."),
        22, INK, True, align=PP_ALIGN.CENTER)


def process_slide(prs, item):
    slide = base(prs, 6, item, 6)
    nodes = item.get("nodes", [
        {"title": "Черновик", "text": "Автор готовит\nинструкцию"},
        {"title": "Reviewer", "text": "Замечания\nи точечные правки"},
        {"title": "Confluence", "text": "Автор публикует\nпроверенную статью"},
        {"title": "RAG", "text": "Поиск\nи ответ с источником"},
        {"title": "Инженер", "text": "Проверяет источник\nи применяет действия"}
    ])
    for i, node in enumerate(nodes):
        x = 0.65 + i * 2.45
        box(slide, x, 2.28, 2.23, 1.88, LIGHT, BORDER)
        txt(slide, x + 0.12, 2.61, 1.99, 0.45, node["title"], 19,
            ACCENT if i in (1, 3) else INK, True, align=PP_ALIGN.CENTER)
        txt(slide, x + 0.12, 3.30, 1.99, 0.61, node["text"], 12.5, MUTED, align=PP_ALIGN.CENTER)
        if i < 4:
            chevron(slide, x + 2.28, 3.07, 0.12, 0.23)
    line(slide, 0.75, 4.53, 5.18, 4.53, ACCENT, 2)
    line(slide, 8.10, 4.53, 12.59, 4.53, ACCENT, 2)
    txt(slide, 0.65, 4.72, 4.73, 0.47, "Качество при создании", 20, INK, True, align=PP_ALIGN.CENTER)
    txt(slide, 7.99, 4.72, 4.69, 0.47, "Скорость при использовании", 20, INK, True, align=PP_ALIGN.CENTER)
    box(slide, 0.65, 5.64, 12.03, 0.90, "EEF0FF", None)
    txt(slide, 0.91, 5.87, 11.51, 0.47,
        item.get("privacy", "Документы и модели — в корпоративном контуре, без публичного LLM-сервиса."),
        18, INK, True, align=PP_ALIGN.CENTER)


def reading_pack(content, out):
    fontdir = Path("/usr/share/fonts/truetype/dejavu")
    pdfmetrics.registerFont(TTFont("Reading", str(fontdir / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont("ReadingBold", str(fontdir / "DejaVuSans-Bold.ttf")))
    heading = ParagraphStyle("heading", fontName="ReadingBold", fontSize=18, leading=23,
                             textColor=colors.HexColor("#182235"), spaceAfter=9)
    body = ParagraphStyle("body", fontName="Reading", fontSize=12, leading=18,
                          textColor=colors.HexColor("#182235"), spaceAfter=11, alignment=TA_LEFT)
    small = ParagraphStyle("small", parent=body, fontSize=9, leading=13,
                           textColor=colors.HexColor("#64738D"))
    flows, markdown = [], [f"# {content.get('title', 'RAG + Reviewer')}\n", "Текст выступления • 5–10 минут\n"]
    for i, item in enumerate(content["slides"], 1):
        if i > 1:
            flows.append(PageBreak())
        flows.append(Paragraph(escape(f"{i}. {item['title']}"), heading))
        if item.get("duration"):
            flows.append(Paragraph(escape(item["duration"]), small))
        notes = item.get("notes", "")
        if isinstance(notes, list):
            notes = "\n\n".join(notes)
        markdown.append(f"\n## {i}. {item['title']}\n")
        if item.get("duration"):
            markdown.append(f"\n{item['duration']}\n")
        markdown.append("\n" + notes + "\n")
        for para in re.split(r"\n\s*\n", notes):
            flows.append(Paragraph(escape(para).replace("\n", "<br/>"), body))
            flows.append(Spacer(1, 2 * mm))
    doc = SimpleDocTemplate(str(out / "reading-text.pdf"), pagesize=(210 * mm, 297 * mm),
                            rightMargin=21 * mm, leftMargin=21 * mm,
                            topMargin=20 * mm, bottomMargin=18 * mm)
    doc.build(flows)
    (out / "reading-text.md").write_text("\n".join(markdown), encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--screens-dir", type=Path, default=ROOT / "docs/presentation/artifacts/2026-10-19/screens")
    p.add_argument("--content-json", type=Path)
    p.add_argument("--output-dir", type=Path, default=ROOT / "docs/presentation/artifacts/2026-10-19")
    p.add_argument("--allow-missing", action="store_true", help="DRAFT ONLY: label missing images prominently")
    p.add_argument("--skip-pdf", action="store_true")
    p.add_argument("--preview", action="store_true", help="Render PDF slides to PNG using pdftoppm")
    p.add_argument("--write-template", type=Path)
    args = p.parse_args()
    if args.write_template:
        args.write_template.parent.mkdir(parents=True, exist_ok=True)
        args.write_template.write_text(json.dumps(DEFAULT, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Content template: {args.write_template}")
        return
    content = json.loads(args.content_json.read_text(encoding="utf-8")) if args.content_json else DEFAULT
    if len(content["slides"]) != 6:
        raise ValueError("This short deck expects exactly six slides")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.screens_dir = args.screens_dir.resolve()
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(W), Inches(H)
    prs.core_properties.title = content.get("title", "RAG + Reviewer")
    prs.core_properties.subject = "Скриншотная демонстрация двух сервисов, 19.10.2026"
    prs.core_properties.author = "Захар Мостовой"
    prs.core_properties.keywords = "RAG, Confluence AI Reviewer, локальные снимки, редактируемые слайды"
    problem_slide(prs, content["slides"][0])
    assets = []
    for i in range(1, 5):
        assets.extend(screen_slide(prs, i + 1, content["slides"][i], args))
    process_slide(prs, content["slides"][5])
    name = "RAG-Reviewer-2026-10-19" + ("-DRAFT" if args.allow_missing else "")
    pptx = args.output_dir / (name + ".pptx")
    prs.save(str(pptx))
    reading_pack(content, args.output_dir)
    (args.output_dir / "build-manifest.json").write_text(json.dumps({
        "title": content.get("title"), "draft": args.allow_missing,
        "slides": 6, "editable_text": True, "images": assets,
        "pptx_sha256": hashlib.sha256(pptx.read_bytes()).hexdigest(),
        "content": content,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"PPTX: {pptx}")
    if not args.skip_pdf:
        with tempfile.TemporaryDirectory(prefix="rag-reviewer-office-20261019-") as tmp:
            subprocess.run(["/usr/bin/libreoffice", f"-env:UserInstallation={Path(tmp).as_uri()}",
                            "--headless", "--convert-to", "pdf", "--outdir", str(args.output_dir),
                            str(pptx)], check=True, timeout=90)
        pdf = pptx.with_suffix(".pdf")
        if not pdf.is_file() or pdf.stat().st_size < 1000:
            raise RuntimeError("LibreOffice did not produce the requested PDF")
        print(f"PDF: {pdf}")
        if args.preview:
            preview_dir = args.output_dir / "previews"
            preview_dir.mkdir(exist_ok=True)
            subprocess.run(["pdftoppm", "-png", "-scale-to", "1600", str(pdf), str(preview_dir / "slide")],
                           check=True, timeout=90)
    print(f"Reading text: {args.output_dir / 'reading-text.pdf'}")


if __name__ == "__main__":
    main()
