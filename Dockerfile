# syntax=docker/dockerfile:1
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    SEATLAB_DB=/data/seatlab.db

WORKDIR /app

# Only what the build backend needs; the demo pages ship inside the wheel.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir ".[api,postgres]" \
 && useradd --create-home --uid 10002 seatlab \
 && chown -R seatlab:seatlab /app

USER seatlab
RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3).status==200 else 1)"

CMD ["seatlab", "serve", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
