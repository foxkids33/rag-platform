FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml ./
RUN uv sync
COPY . .
ENV PATH="/app/.venv/bin:$PATH"
CMD ["python", "-m", "app.main"]
