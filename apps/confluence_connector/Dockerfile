FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml ./
COPY confluence_connector ./confluence_connector
RUN pip install --no-cache-dir .

RUN useradd --system --uid 10001 connector
USER connector
CMD ["python", "-m", "confluence_connector"]
