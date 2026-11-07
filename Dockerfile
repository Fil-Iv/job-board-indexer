FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so they are cached between code changes.
COPY pyproject.toml README.md ./
COPY jobindex/__init__.py jobindex/__init__.py
RUN pip install -e ".[dev]"

COPY . .

EXPOSE 8000
CMD ["uvicorn", "jobindex.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
