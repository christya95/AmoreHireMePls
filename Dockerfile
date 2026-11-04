# Official Playwright Python image: Chromium + system deps matched to playwright==1.64.x, Python 3.12.
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

# LibreOffice renders DOCX -> PDF for page-count verification. Fonts for Calibri-like metrics.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libreoffice-writer fonts-liberation fonts-crosextra-carlito git \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY config ./config
RUN uv sync --frozen --no-dev --no-editable

# Non-root runtime user with a writable data volume.
RUN useradd --create-home --uid 10001 amore \
 && mkdir -p /data/artifacts /data/private /data/logs \
 && chown -R amore:amore /data /app
USER amore
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

VOLUME ["/data"]
EXPOSE 8790
CMD ["amore", "web"]
