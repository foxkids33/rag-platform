FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Local CPU OCR; no SaaS OCR, external image fetches or GPU model downloads.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-rus fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

ARG WORKER_INSTALL_DOCLING=true
RUN if [ "$WORKER_INSTALL_DOCLING" = "true" ]; then \
        apt-get update && apt-get install -y --no-install-recommends \
        g++ \
        libxcb1 \
        libx11-6 \
        libxext6 \
        libxrender1 \
        libgl1 \
        libglib2.0-0 \
        && rm -rf /var/lib/apt/lists/*; \
    fi

RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
RUN if [ "$WORKER_INSTALL_DOCLING" = "true" ]; then \
        uv sync --frozen --extra docling; \
    else \
        uv sync --frozen; \
    fi
COPY . .
ENV PATH="/app/.venv/bin:$PATH"
CMD ["python", "-m", "app.main"]
