# syntax=docker/dockerfile:1
# One image, both transports:
#   docker run -p 8000:8000 IMAGE         -> HTTP on $PORT (default 8000; GET /health)
#   docker run -i IMAGE stdio             -> stdio worker, for DuckDB on the same host
FROM python:3.13-slim

ARG VERSION=0.0.0
ARG GIT_COMMIT=unknown
LABEL org.opencontainers.image.title="vgi-starter-worker" \
      org.opencontainers.image.description="A VGI worker for DuckDB" \
      org.opencontainers.image.source="https://github.com/Query-farm/vgi-starter-python" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.licenses="0BSD"

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PORT=8000
WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv pip install --system --no-cache . && rm /usr/local/bin/uv

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 worker
USER worker

COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD python -c "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health',timeout=2)"
ENTRYPOINT ["docker-entrypoint.sh"]
