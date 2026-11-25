# Как документ превращается в результат поиска

При загрузке API сохраняет файл в MinIO, затем регистрирует документ и задание в PostgreSQL. HTTP 201 со статусом `QUEUED` означает, что загрузка принята. Воркер скачивает оригинал по ключу, вызывает парсер в отдельном процессе и передаёт в модель эмбеддингов поле `search_text` каждого дочернего чанка. Запись фрагментов в `document_chunks` и перевод документа в `READY` завершают индексацию. [Загрузка](../apps/api/app/api/routes/documents.py) · [воркер](../apps/worker/app/main.py) · [процесс парсера](../apps/worker/app/parser_process.py)

| Формат | Что делает воркер | Код |
| --- | --- | --- |
| TXT, CSV, JSON, HTML, XML | Декодирует текст. CSV раскладывает по ячейкам, JSON форматирует, HTML и XML очищает от разметки. Затем ищет удобную границу в конце окна текста. | [extract_text, split_into_chunks](../apps/worker/app/text_processing.py) |
| Markdown | Прозу делит как обычный текст. Блок кода в ограждении из обратных кавычек или тильд сохраняет целиком, включая отступы. | [_parse_markdown](../apps/worker/app/document_processing.py) |
| DOCX | Читает word/document.xml из архива OpenXML. Собирает абзацы и строки таблиц с учётом иерархии заголовков. Новый раздел или превышение целевого размера завершает группу. | [_parse_docx](../apps/worker/app/document_processing.py) |
| PDF и изображения | PDFium извлекает текст по страницам. При необходимости Tesseract распознаёт скан. Для изображений и графики при включённом vision доступно описание моделью. Полученный текст делится в пределах страницы. В метаданных сохраняется её номер. | [extract_pdf, extract_image](../apps/worker/app/media_processing.py) · [_media_chunks](../apps/worker/app/document_processing.py) |
| Confluence | Коннектор получает страницу в storage-формате. API сохраняет версию, URL, пространство и содержимое. Воркер разбирает заголовки, списки, таблицы и макросы кода. Блоки с одним путём заголовков группируются до целевого размера. | [Чтение страницы](../apps/confluence_connector/confluence_connector/confluence.py) · [импорт](../apps/api/app/api/routes/confluence.py) · [разбор storage](../apps/worker/app/confluence_processing.py) · [_parse_confluence](../apps/worker/app/document_processing.py) |

## Как выбирается граница

У обычного текста целевое окно — 2200 **символов**. Во второй половине окна алгоритм ищет последнюю доступную границу абзаца, строки, предложения или слова. Следующее окно начинается за 300 символов до конца предыдущего. Короткий хвост меньше 180 символов присоединяется к предыдущему чанку. Настройки задают `CHUNK_MAX_CHARS`, `CHUNK_OVERLAP_CHARS` и `CHUNK_MIN_CHARS`. Для структурных блоков DOCX и Confluence действуют свои правила группировки. Целый блок кода или большая таблица могут превысить ориентир. [Алгоритм](../apps/worker/app/text_processing.py) · [параметры](../apps/worker/app/config.py)

Допустим, на странице «Установка» есть разделы «Требования» и «Проверка». В первом перечислены версии пакетов, во втором приведена команда проверки. На смене раздела алгоритм закроет текущий чанк. К тексту команды для поиска добавится путь «Установка > Проверка». У найденного фрагмента сохранятся ссылка и версия страницы. Соседние дочерние фрагменты одного раздела могут получить общий родительский текст до 12 000 символов по умолчанию. Это даёт построителю ответа более широкий контекст вокруг точного совпадения. [Сборка родителей](../apps/worker/app/document_processing.py) · [использование контекста](../apps/api/app/services/context_builder.py)

## Как посмотреть на своём файле

Из корня репозитория выполните команду на тестовом документе. Она запускает тот же выбор парсера и печатает результат локально.

```bash
cd apps/worker
uv run --frozen python - /путь/к/примеру.md <<'PY'
import sys
from pathlib import Path
from app.document_processing import parse_document

p = Path(sys.argv[1])
for i, c in enumerate(parse_document(p.name, p.read_bytes()), 1):
    print(i, c.metadata["parser"], len(c.text), c.heading_breadcrumb, c.page_start)
    print(c.search_text[:500])
PY
```

На стенде PDF обрабатывает PDFium, DOCX — парсер OpenXML. В исходниках есть отдельная функция `_parse_with_docling` с `HybridChunker` и настройкой 450 токенов. Диспетчер `parse_document` её не вызывает. Профиль Docker может установить зависимости Docling, алгоритм выбора парсера от этого не меняется. [Диспетчер и функция Docling](../apps/worker/app/document_processing.py) · [примеры на реальных форматах](../apps/worker/tests/test_formats_and_images.py)
