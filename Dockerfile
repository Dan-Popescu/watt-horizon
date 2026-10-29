# syntax=docker/dockerfile:1
# Read-only web/MCP server and scheduled jobs. State lives in the /data volume.
#   docker build -t radar .
#   docker run -p 8765:8765 -v radar-data:/data -e RADAR_API_TOKEN=... radar
FROM python:3.12-slim AS base

# Tesseract (French) for scanned pages; transcripts in /data/transcripts take precedence.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-fra \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH"

WORKDIR /app
# Dependencies first (cached layer), then the project itself.
COPY pyproject.toml uv.lock ./
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    uv sync --frozen --no-dev --extra llm --no-install-project
COPY src ./src
COPY gold ./gold
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    uv sync --frozen --no-dev --extra llm --no-editable

RUN useradd --create-home --uid 10001 radar \
 && mkdir -p /data/gold /backups \
 && cp -a /app/gold/. /data/gold/ \
 && chown -R radar:radar /data /backups
USER radar
ENV RADAR_DATA_DIR=/data RADAR_GOLD_DIR=/data/gold PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4)"
# Listening on 0.0.0.0 requires RADAR_API_TOKEN (the server refuses otherwise).
CMD ["radar", "web", "--host", "0.0.0.0", "--port", "8765"]
