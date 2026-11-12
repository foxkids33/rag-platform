#!/usr/bin/env python3
"""Build an editable, native-shape dark deck from the approved seven-slide outline.

This is an artifact build, not a new model experiment. The small measured samples
and their scope are kept explicit. Speaker notes are attached by the reading pack.
"""

from pathlib import Path
import argparse

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE, MSO_CONNECTOR
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / ".storage/presentation/knowledge-dark-native-v4-source.pptx"
WIDTH, HEIGHT = 13.333333, 7.5
FONT = "DejaVu Sans"
MONO = "DejaVu Sans Mono"
BG = "0B1220"
PANEL = "142035"
PANEL_2 = "192940"
LINE = "304159"
WHITE = "F8FAFC"
MUTED = "BDCADB"
FAINT = "8295AE"
TEAL = "2DD4BF"
VIOLET = "A78BFA"
AMBER = "FBBF24"
PINK = "FB7185"


def register_fonts():
    fonts = {
        "Deck": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "DeckBold": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "DeckMono": "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    }
    for name, path in fonts.items():
        pdfmetrics.registerFont(TTFont(name, path))


def rgb(value):
    return RGBColor.from_string(value)


def rect(slide, x, y, w, h, fill=PANEL, line=None, radius=True):
    shape = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE,
        Inches(x), Inches(y), Inches(w), Inches(h),
    )
    if radius:
        shape.adjustments[0] = 0.10
    shape.fill.solid()
    shape.fill.fore_color.rgb = rgb(fill)
    if line:
        shape.line.color.rgb = rgb(line)
        shape.line.width = Pt(1)
    else:
        shape.line.fill.background()
    return shape


def text(slide, x, y, w, h, value, size=22, color=WHITE, bold=False,
         align=PP_ALIGN.LEFT, mono=False, valign=MSO_ANCHOR.TOP):
    # Manual line breaks are intentional. Fit the longest line to its own box
    # rather than letting PowerPoint wrap identifiers a character at a time.
    measure_font = "DeckMono" if mono else "DeckBold" if bold else "Deck"
    longest = max(pdfmetrics.stringWidth(line, measure_font, size)
                  for line in value.split("\n"))
    available = w * 72 - 3
    if longest > available:
        size = max(12, size * available / longest)
    shape = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    frame = shape.text_frame
    frame.clear()
    frame.margin_left = frame.margin_right = 0
    frame.margin_top = frame.margin_bottom = 0
    frame.word_wrap = False
    frame.vertical_anchor = valign
    for index, line in enumerate(value.split("\n")):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        paragraph.text = line
        paragraph.alignment = align
        paragraph.space_before = Pt(0)
        paragraph.space_after = Pt(0)
        paragraph.line_spacing = 1.15
        for run in paragraph.runs:
            run.font.name = MONO if mono else FONT
            run.font.size = Pt(size)
            run.font.bold = bold
            run.font.color.rgb = rgb(color)
    return shape


def rule(slide, x1, y1, x2, y2, color=LINE, width=1.4):
    shape = slide.shapes.add_connector(
        MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2))
    shape.line.color.rgb = rgb(color)
    shape.line.width = Pt(width)
    return shape


def arrow(slide, x, y, w=0.22, color=FAINT):
    shape = slide.shapes.add_shape(
        MSO_SHAPE.CHEVRON, Inches(x), Inches(y), Inches(w), Inches(0.23))
    shape.fill.solid()
    shape.fill.fore_color.rgb = rgb(color)
    shape.line.fill.background()
    return shape


def pill(slide, x, y, w, label, accent=TEAL, fill=PANEL_2, size=15):
    rect(slide, x, y, w, 0.40, fill, line=LINE)
    text(slide, x + 0.10, y + 0.075, w - 0.20, 0.27, label,
         size=size, color=accent, bold=True, align=PP_ALIGN.CENTER)


def slide_base(prs, number, title, subtitle, accent=TEAL):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = rgb(BG)
    # Small accents, not raster backgrounds: all elements stay editable.
    rect(slide, 0.55, 0.35, 0.43, 0.055, accent, radius=False)
    text(slide, 1.10, 0.285, 11.0, 0.26,
         "ТЕХНИЧЕСКАЯ ДОКУМЕНТАЦИЯ  /  REVIEWER + RAG",
         size=11, color=FAINT)
    text(slide, 0.55, 0.85, 12.20, 0.64, title, size=34, bold=True)
    text(slide, 0.55, 1.58, 12.20, 0.42, subtitle, size=17, color=MUTED)
    rule(slide, 0.55, 7.01, 12.78, 7.01, color=LINE, width=0.8)
    text(slide, 0.55, 7.14, 10.80, 0.22,
         "CONFLUENCE  /  КАЧЕСТВО ЗНАНИЙ И ПОИСК ОТВЕТОВ", size=9, color=FAINT)
    text(slide, 12.15, 7.11, 0.63, 0.27, f"{number:02d} / 07",
         size=10, color=FAINT, align=PP_ALIGN.RIGHT)
    return slide


def role_card(slide, x, title, tagline, lines, accent, marker):
    rect(slide, x, 2.20, 5.94, 3.45, PANEL, line=LINE)
    rect(slide, x + 0.24, 2.46, 0.63, 0.63, PANEL_2)
    text(slide, x + 0.24, 2.57, 0.63, 0.39, marker, size=22,
         color=accent, bold=True, align=PP_ALIGN.CENTER)
    text(slide, x + 1.05, 2.51, 4.48, 0.47, title, size=26, bold=True)
    text(slide, x + 0.28, 3.30, 5.30, 0.75, tagline, size=22, color=accent)
    text(slide, x + 0.28, 4.28, 5.33, 0.98, lines, size=19, color=MUTED)


def build_problem(prs):
    s = slide_base(prs, 1, "Задача", "От воспроизводимой инструкции — к подходящему ответу")
    role_card(s, 0.55, "Автор", "Подготовить понятную\ntroubleshooting-инструкцию",
              "Условия применения и полные действия\nОжидаемый результат и проверка", VIOLET, "01")
    role_card(s, 6.84, "Инженер", "Найти нужный случай\nи действия вместе с источником",
              "Без потерянных шагов и параметров\nС возможностью сверить документ", TEAL, "02")
    rect(s, 0.55, 5.98, 12.23, 0.71, PANEL_2)
    text(s, 0.80, 6.13, 11.71, 0.40,
         "Структура источника + полный код + проверяемая ссылка", size=24, bold=True)
    text(s, 0.55, 6.77, 12.23, 0.20,
         "Документы и модель размещаются в корпоративной сети, без публичного LLM-сервиса.",
         size=12, color=MUTED)


def build_cycle(prs):
    s = slide_base(prs, 2, "Цикл работы со знаниями", "Два приложения — две стороны одного процесса", VIOLET)
    labels = [
        ("Автор", "Черновик", MUTED),
        ("Reviewer", "Замечания", VIOLET),
        ("Решение\nавтора", "Правка и публикация", VIOLET),
        ("Confluence", "Статья", MUTED),
        ("RAG", "Поиск и ответ", TEAL),
        ("Инженер", "Применение", TEAL),
    ]
    w, gap = 1.83, 0.25
    for i, (label, descriptor, accent) in enumerate(labels):
        x = 0.55 + i * (w + gap)
        rect(s, x, 2.75, w, 1.50, PANEL, line=LINE)
        text(s, x + 0.11, 3.01, w - 0.22, 0.76, label, size=22,
             color=accent, bold=True, align=PP_ALIGN.CENTER,
             valign=MSO_ANCHOR.MIDDLE)
        text(s, x - 0.03, 4.43, w + 0.06, 0.46, descriptor, size=13,
             color=MUTED, align=PP_ALIGN.CENTER)
        if i < 5:
            arrow(s, x + w + 0.055, 3.38, 0.14)
    rule(s, 0.60, 5.25, 6.48, 5.25, color=VIOLET, width=2)
    rule(s, 6.79, 5.25, 12.76, 5.25, color=TEAL, width=2)
    text(s, 0.55, 5.46, 5.98, 0.52, "Качество при создании", size=25,
         color=VIOLET, bold=True, align=PP_ALIGN.CENTER)
    text(s, 6.79, 5.46, 5.98, 0.52, "Доступность при использовании", size=25,
         color=TEAL, bold=True, align=PP_ALIGN.CENTER)
    text(s, 0.55, 6.36, 12.23, 0.45,
         "Связующее звено — Confluence. Публикация остаётся решением автора.",
         size=18, color=MUTED, align=PP_ALIGN.CENTER)


def build_documents(prs):
    s = slide_base(prs, 3, "RAG: подготовка документов", "Storage XHTML → структурные блоки → child / parent → индекс")
    rect(s, 0.55, 2.24, 2.68, 3.34, PANEL, line=LINE)
    text(s, 0.80, 2.53, 2.18, 0.42, "Confluence", size=23, bold=True)
    text(s, 0.80, 3.13, 2.18, 1.46,
         "Заголовки и списки\nТаблицы\nКод и макросы", size=19, color=MUTED)
    pill(s, 0.80, 4.75, 2.18, "Storage XHTML", size=14)
    arrow(s, 3.37, 3.80, 0.20)
    rect(s, 3.73, 2.24, 5.01, 3.34, PANEL, line=TEAL)
    text(s, 3.99, 2.49, 4.49, 0.39, "Parent: окружающий раздел", size=22,
         bold=True, color=TEAL)
    rect(s, 3.99, 3.12, 4.49, 0.51, PANEL_2)
    text(s, 4.15, 3.22, 4.14, 0.31, "Child · небольшой фрагмент для поиска", size=17)
    rect(s, 3.99, 3.82, 4.49, 0.83, BG, line=LINE)
    text(s, 4.15, 3.99, 4.13, 0.51, "Child · полный блок команд\nбез разрезания строк и переносов",
         size=16, color=TEAL, mono=True)
    text(s, 3.99, 4.96, 4.49, 0.40,
         "Найдённый child → расширение до parent", size=17, color=MUTED)
    arrow(s, 8.91, 3.80, 0.20)
    rect(s, 9.28, 2.24, 3.50, 3.34, PANEL, line=LINE)
    text(s, 9.53, 2.53, 2.99, 0.42, "Индекс и источник", size=22, bold=True)
    text(s, 9.53, 3.13, 2.99, 1.44,
         "PostgreSQL / pgvector\nMinIO: оригиналы\nURL и версия страницы", size=18, color=MUTED)
    pill(s, 9.53, 4.75, 2.99, "Отдельный worker", size=14)
    rect(s, 0.55, 5.90, 12.23, 0.83, PANEL_2)
    text(s, 0.80, 6.08, 11.70, 0.47,
         "Изображения и нераскрытые схемы: явная отметка, не OCR и не vision.",
         size=19, color=MUTED)


def build_search(prs):
    s = slide_base(prs, 4, "RAG: поиск и проверяемый ответ", "Смысл + слова + точные обозначения — объединение по рангам")
    inputs = [(2.27, "Смысл", "BGE-M3"),
              (3.43, "Слова", "PostgreSQL FTS"),
              (4.59, "Точные имена", "Файлы, сервисы, ID")]
    for y, label, detail in inputs:
        rect(s, 0.55, y, 2.73, 0.91, PANEL, line=LINE)
        text(s, 0.77, y + 0.12, 2.30, 0.33, label, size=21, bold=True, color=TEAL)
        text(s, 0.77, y + 0.51, 2.30, 0.24, detail, size=13, color=MUTED)
        rule(s, 3.28, y + 0.46, 3.55, y + 0.46, color=TEAL)
    rule(s, 3.55, 2.73, 3.55, 5.05, color=TEAL)
    rule(s, 3.55, 3.89, 3.73, 3.89, color=TEAL)
    nodes = [(3.82, 1.30, "RRF", "Ранги"),
             (5.52, 1.86, "Контекст", "Child → parent\nПроверка ID"),
             (7.78, 1.27, "LLM", "Ответ")]
    for x, w, label, detail in nodes:
        rect(s, x, 3.22, w, 1.35, PANEL, line=LINE)
        text(s, x + 0.10, 3.53, w - 0.20, 0.42, label, size=22,
             bold=True, align=PP_ALIGN.CENTER)
        text(s, x + 0.10, 3.98, w - 0.20, 0.48, detail, size=12,
             color=MUTED, align=PP_ALIGN.CENTER)
        arrow(s, x + w + 0.12, 3.79, 0.16, color=TEAL)
    rect(s, 9.46, 2.27, 3.32, 3.23, PANEL, line=TEAL)
    text(s, 9.69, 2.60, 2.85, 0.82, "Проверка\nполного блока", size=24,
         bold=True, color=TEAL)
    text(s, 9.69, 3.70, 2.85, 1.27,
         "Дословное совпадение\nURL · версия · SHA-256\nИз переданного контекста", size=16, color=MUTED)
    pill(s, 0.55, 5.93, 1.27, "k = 60", size=15)
    pill(s, 2.00, 5.93, 1.82, "exact × 3", size=15)
    pill(s, 4.00, 5.93, 4.12, "reranker: выключен в demo", size=15)
    text(s, 0.55, 6.51, 12.23, 0.37,
         "Дословность блока ≠ полнота всей процедуры. Неподтверждённый дословный код не выдаётся.",
         size=15, color=MUTED)


def build_requirements(prs):
    s = slide_base(prs, 5, "Reviewer: требования к статье", "10 критериев корпоративного чек-листа — применимость по контексту", VIOLET)
    cards = [
        (0.55, "01", "Применимость", "Симптомы и окружение\nУсловия применения\nЗависимости"),
        (4.75, "02", "Действия", "Техническая точность\nИсполнимость\nПредупреждения"),
        (8.95, "03", "Проверка", "Ожидаемый результат\nВосстановление\nИли эскалация"),
    ]
    for x, num, title, body in cards:
        rect(s, x, 2.23, 3.83, 2.91, PANEL, line=LINE)
        text(s, x + 0.25, 2.48, 3.32, 0.36, num, size=20, color=VIOLET, bold=True)
        text(s, x + 0.25, 3.06, 3.32, 0.48, title, size=25, bold=True)
        text(s, x + 0.25, 3.79, 3.32, 1.00, body, size=18, color=MUTED)
    rect(s, 0.55, 5.47, 5.93, 0.91, PANEL_2)
    text(s, 0.79, 5.61, 5.40, 0.30, "REQUIRED", size=16, color=VIOLET, bold=True)
    text(s, 0.79, 6.00, 5.40, 0.29, "Конкретная существенная проблема", size=17)
    rect(s, 6.85, 5.47, 5.93, 0.91, PANEL_2)
    text(s, 7.09, 5.61, 5.40, 0.30, "UNCERTAIN", size=16, color=AMBER, bold=True)
    text(s, 7.09, 6.00, 5.40, 0.29, "Недостаток сведений — без готовой замены", size=17)
    text(s, 0.55, 6.64, 12.23, 0.27,
         "10 категорий анализа — не 10 обязательных замечаний и не одинаковые заголовки.",
         size=14, color=MUTED)


def build_edits(prs):
    s = slide_base(prs, 6, "Reviewer: команды и правки", "Несохранённый черновик → два последовательных прохода одной модели", VIOLET)
    labels = [("Снимок", 2.18), ("Общий анализ", 2.34),
              ("Команды", 2.13), ("source guard", 2.22), ("Apply / Undo", 2.28)]
    x = 0.55
    for i, (label, w) in enumerate(labels):
        rect(s, x, 2.20, w, 0.67, PANEL, line=LINE)
        text(s, x + 0.12, 2.37, w - 0.24, 0.35, label, size=18,
             color=VIOLET, bold=True, align=PP_ALIGN.CENTER)
        if i < len(labels) - 1:
            arrow(s, x + w + 0.09, 2.42, 0.14, color=VIOLET)
        x += w + 0.25
    rect(s, 0.55, 3.21, 5.35, 2.70, PANEL, line=LINE)
    text(s, 0.81, 3.48, 4.85, 0.44, "Привязка и применение", size=25, bold=True)
    text(s, 0.81, 4.24, 4.85, 1.37,
         "Цитата и source_id\nПроверка актуального snapshot\nТочечный DOM-патч + native Undo", size=19, color=MUTED)
    rect(s, 6.24, 3.21, 6.54, 2.70, PANEL, line=VIOLET)
    text(s, 6.52, 3.48, 5.95, 0.44, "Минимальная правка", size=25, bold=True)
    rect(s, 6.51, 4.20, 5.98, 1.08, BG)
    text(s, 6.70, 4.37, 5.57, 0.38,
         "− systmctl status example.service", size=19, color=PINK, mono=True)
    text(s, 6.70, 4.81, 5.57, 0.38,
         "+ systemctl status example.service", size=19, color=TEAL, mono=True)
    text(s, 6.51, 5.55, 5.99, 0.24, "Условный сервис; исправляется конкретный фрагмент.",
         size=12, color=MUTED)
    text(s, 0.55, 6.38, 12.23, 0.45,
         "Команды не исполняются. Автор выбирает правку; сохранение и публикация — отдельно.",
         size=17, color=MUTED)


def metric(slide, x, y, value, label, detail, accent, value_size=35):
    rect(slide, x, y, 5.93, 1.28, PANEL, line=LINE)
    rect(slide, x + 0.01, y + 0.13, 0.04, 1.00, accent, radius=False)
    text(slide, x + 0.23, y + 0.13, 5.43, 0.55, value,
         size=value_size, color=accent, bold=True)
    text(slide, x + 0.23, y + 0.69, 5.43, 0.27, label, size=16, bold=True)
    text(slide, x + 0.23, y + 1.02, 5.43, 0.22, detail, size=11.7, color=MUTED)


def build_results(prs):
    s = slide_base(prs, 7, "Проверки и результаты", "Сохранённые проверки 7–8 октября — объём каждого сценария указан")
    text(s, 0.55, 2.13, 5.93, 0.38, "RAG", size=22, bold=True, color=TEAL)
    text(s, 6.85, 2.13, 5.93, 0.38, "Reviewer", size=22, bold=True, color=VIOLET)
    metric(s, 0.55, 2.64, "8/8", "Ожидаемая страница в top-5",
           "8 положительных вопросов · 3 режима · по 4 статьи в поиске", TEAL)
    metric(s, 0.55, 4.00, "63 мс", "Медиана hybrid retrieval",
           "n=9 запросов · только поиск, без генерации LLM", TEAL)
    metric(s, 0.55, 5.36, "4/4", "Дословное совпадение блоков кода",
           "Один подготовленный ответ · не полнота всех процедур", TEAL)
    metric(s, 6.85, 2.64, "11/11", "Операции редактора",
           "UI / Apply / Undo / stale · часть отчётов детерминированная", VIOLET)
    metric(s, 6.85, 4.00, "9/9  ·  12/12", "Backend-сбои  /  права и XSRF",
           "Transport stub  /  контролируемые проверки Confluence", VIOLET, value_size=30)
    metric(s, 6.85, 5.36, "45,6 / 72,4 с", "p50 / p95 полного review",
           "n=40 синтетических случаев · один development-прогон", VIOLET, value_size=30)
    # Footer is deliberately separated from metric cards and remains on canvas.
    text(s, 0.55, 6.78, 12.23, 0.22,
         "Не общая точность системы, не SLA и не сравнение с Just AI; время retrieval отдельно от LLM.",
         size=12, color=MUTED)


def verify_bounds(prs):
    for index, slide in enumerate(prs.slides, start=1):
        for shape in slide.shapes:
            if (shape.left < 0 or shape.top < 0
                    or shape.left + shape.width > prs.slide_width + 2
                    or shape.top + shape.height > prs.slide_height + 2):
                raise ValueError(f"Slide {index}: shape out of bounds: {shape.name}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    register_fonts()
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(WIDTH), Inches(HEIGHT)
    prs.core_properties.title = "Техническая документация: от проверки статьи к поиску ответа"
    prs.core_properties.subject = "Reviewer и RAG: подготовка, поиск, проверяемый код"
    prs.core_properties.author = "Zakhar Mostovoy"
    prs.core_properties.keywords = "Confluence, Reviewer, RAG, troubleshooting"
    for build in (build_problem, build_cycle, build_documents, build_search,
                  build_requirements, build_edits, build_results):
        build(prs)
    verify_bounds(prs)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    prs.save(args.output)
    print(f"Saved {len(prs.slides)} editable slides: {args.output}")


if __name__ == "__main__":
    main()
