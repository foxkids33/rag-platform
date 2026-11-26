# Обработка документов и изображений

Worker скачивает исходник из закрытого object storage, извлекает текст,
строит parent/child chunks и индексирует их. Документ становится `READY`
только после успешного извлечения и сохранения embeddings. Пустой,
повреждённый или недоступный для распознавания файл не считается готовым.

## Форматы

- TXT: UTF-8/UTF-8 BOM, UTF-16/32 с BOM, Windows-1251.
- Markdown: текст и целые fenced code blocks, без исполнения кода.
- CSV: разделители comma/semicolon/tab/pipe, quoted cells.
- JSON: строгий разбор; XML: безопасный разбор без DTD/entities.
- HTML/HTM: видимый текст; script/style/template не индексируются.
- DOCX: OpenXML paragraphs, headings, tables; без макросов и внешних ссылок.
- PDF: локальный PDFium, текстовый слой; сканы — локальный Tesseract.
- PNG, JPEG/JPG, WebP, TIFF/TIF: OCR; при настройке vision — также описание
  схемы/изображения моделью. Многостраничный TIFF обрабатывается по кадрам;
  анимация не поддерживается.

PDF больше не требует Docling, Torch и загрузки моделей при первом запросе.
Контейнеры `Dockerfile` и `Dockerfile.clean` включают Tesseract `rus` + `eng`.
Изображения внутри Confluence не загружаются по URL: это отдельная задача
коннектора и проверки прав доступа, не скрытый внешний запрос worker.
Изображения внутри DOCX сейчас не анализируются; поддерживается текстовое
содержимое DOCX и отдельные приложенные изображения.

## OCR и vision

```dotenv
OCR_ENABLED=true
OCR_LANGUAGES=rus+eng
OCR_TIMEOUT_SECONDS=20
OCR_TOTAL_TIMEOUT_SECONDS=120
OCR_MAX_PAGES=10
# 0 снимает лимит числа страниц PDF. Положительное число задаёт лимит.
PDF_MAX_PAGES=0
IMAGE_MAX_PIXELS=16000000
IMAGE_MAX_FRAMES=10

# По умолчанию отключено. Нужна реально совместимая multimodal-модель.
VISION_ENABLED=false
VISION_BASE_URL=https://corporate-vision.example.internal
VISION_MODEL=your-vision-model
# VISION_API_KEY задаётся только в приватном env/secret, не в репозитории.
VISION_ALLOW_INSECURE_HTTP=false
VISION_TIMEOUT_SECONDS=30
VISION_MAX_IMAGES_PER_DOCUMENT=4
# Для корпоративного Qwen без отдельного reasoning-прохода:
VISION_ENABLE_THINKING=false
```

Vision использует только настроенный OpenAI-compatible endpoint
`/v1/chat/completions` и собственные bytes через data URL. Worker не
загружает image URL из документов, не следует HTTP redirects и не использует
HTTP_PROXY для отправки изображений. Для явно разрешённого внутреннего HTTP
endpoint нужен `VISION_ALLOW_INSECURE_HTTP=true`. Простой text-only LLM не
становится vision-моделью от включения этого флага.

OCR и модельное описание разделены в тексте и metadata:
`extraction_methods`, `derived_content`, `warnings`, `vision_model`.
OCR может путать символы, особенно смешанную кириллицу/латиницу и мелкие
надписи. Описание модели является интерпретацией. Дословное совпадение с
OCR-текстом не доказывает совпадение команды с исходными пикселями.

Если vision недоступен, существующий OCR сохраняется с предупреждением.
Если нет ни читаемого текста, ни описания, файл завершается с понятной
ошибкой — не `READY` с выдуманным содержимым. Бюджет vision ограничен;
превышение количества анализируемых изображений отмечается предупреждением.

## Ограничения безопасности

Нормальный ingestion запускает парсер отдельным процессом, с JSON IPC
(не pickle), ограничениями памяти/CPU и wall-clock timeout. По истечении
timeout завершается вся process group, включая OCR subprocess.

```dotenv
PARSER_MAX_INPUT_MB=100
PARSER_MAX_TEXT_CHARS=2000000
PARSER_TIMEOUT_SECONDS=240
PARSER_MEMORY_MB=1536
PARSER_CPU_SECONDS=120
PDF_TIMEOUT_SECONDS=3600
```

Воркер разбирает PDF последовательно, в отдельных процессах по 10 исходных
страниц (или меньше, если `OCR_MAX_PAGES` ниже 10). В каждой группе свой
бюджет времени OCR, CPU и памяти. На всю книгу действуют пределы текста,
ответа парсера, вызовов vision и общее время `PDF_TIMEOUT_SECONDS`.
Исходная нумерация страниц сохраняется. Воркер приступает к эмбеддингам
после разбора последней группы. Ошибка на поздней странице завершает задание
целиком. Для TIFF по-прежнему действует единый OCR-бюджет на файл.
Дополнительно ограничены image pixels/frames, размер и распаковка
DOCX XML/ZIP. Неправильное расширение изображения не принимается за другой
формат; SVG и анимированные изображения не разрешены. PDF JavaScript,
Office macros и команды документов не исполняются. Эти меры не заменяют
обновления native libraries и лимиты/изоляцию самого контейнера.

## Проверки

```bash
cd apps/worker
uv run --frozen pytest -q
```

Тесты создают действительные PDF, DOCX, raster images и все текстовые
форматы; проверяют native PDF, PDF-scan, OCR, vision mock, неверные файлы,
XML entities, ZIP/pixel/page limits, subprocess failure/timeout. Три
настоящие OCR-проверки требуют установленный Tesseract и `rus`/`eng`; в
готовом Docker image они доступны. Тесты не читают корпоративные статьи,
не вызывают настоящую LLM и не изменяют PostgreSQL/object storage.
