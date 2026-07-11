FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOUYIN_DATA_DIR=/data \
    DOUYIN_WEB_HOST=0.0.0.0 \
    DOUYIN_WEB_PORT=8000 \
    WEB_CONCURRENCY=1

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        nodejs \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src

RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 recorder \
    && mkdir -p /data/recordings \
    && chown -R recorder:recorder /data

USER recorder

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

CMD ["python", "-m", "douyin_recorder"]
