# Two targets:
#   dev  (default, used by docker-compose.yml): dev tools, source mounted as a volume
#   prod (docker-compose.prod.yml): runtime dependencies only, code baked in, non-root
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so they are cached between code changes.
COPY pyproject.toml README.md ./
COPY jobindex/__init__.py jobindex/__init__.py
RUN pip install -e .

EXPOSE 8000

# --- production --------------------------------------------------------------
FROM base AS prod
COPY alembic.ini ./
COPY migrations migrations
COPY jobindex jobindex
RUN useradd --system --uid 10001 --no-create-home jobindex
USER jobindex
CMD ["uvicorn", "jobindex.api.app:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]

# --- development (default target) ---------------------------------------------
FROM base AS dev
RUN pip install -e ".[dev]"
# Optional headless Chromium for browser-rendered connectors (jobindex/browser.py):
#   docker compose build --build-arg INSTALL_BROWSER=1 app
ARG INSTALL_BROWSER=0
RUN if [ "$INSTALL_BROWSER" = "1" ]; then \
        apt-get update && apt-get install -y --no-install-recommends libatomic1 \
        && pip install -e ".[browser]" && playwright install --with-deps chromium \
        && rm -rf /var/lib/apt/lists/*; \
    fi
COPY . .
CMD ["uvicorn", "jobindex.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
