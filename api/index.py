"""Vercel entry point: the FastAPI app (JSON API, search page, /docs) as one Python
function. pyproject.toml [tool.vercel] points the FastAPI preset here; see docs/DEPLOY.md.

Vercel sets ``VERCEL=1``, which switches jobindex.config to serverless defaults: no
connection pool (the Supabase transaction pooler pools), READ ONLY transactions and
CDN caching of API responses. Nothing here crawls or schedules: crawling runs in GitHub
Actions (.github/workflows/crawl.yml).
"""

from jobindex.api.app import app

__all__ = ["app"]
