"""Application settings, read from environment variables (and an optional .env file)."""

from functools import lru_cache
from typing import Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://jobindex:jobindex@localhost:5432/jobindex"
    # Connection tuning (jobindex/db.py). Defaults suit docker-compose / a VPS; the
    # hosted demo (docs/DEPLOY.md) goes through the Supabase pooler.
    #   None = auto: server-side prepared statements are disabled for pooler URLs
    #   (*.pooler.supabase.com, port 6543, ?pgbouncer=true), which do not support them.
    database_prepared_statements: bool | None = None
    database_pool_size: int | None = None  # None = CRAWL_CONCURRENCY + 2 (at least 5)
    database_max_overflow: int = 10
    database_pool_timeout_seconds: float = 30.0
    # Serverless (Vercel): no pool kept between requests; the pooler does the pooling.
    database_null_pool: bool = False
    # Every transaction is READ ONLY (the public API never writes).
    database_read_only: bool = False

    # API responses (not /health) may be cached this long by a CDN (Cache-Control
    # s-maxage); 0 = no Cache-Control header.
    api_cache_seconds: int = 0
    # Set to "1" by Vercel at runtime. Switches the defaults of database_null_pool,
    # database_read_only (both true) and api_cache_seconds (60) unless set explicitly.
    vercel: bool = False

    # HTTP / crawling etiquette
    http_user_agent: str = "JobIndexBot/0.1 (+https://example.com/jobindex-bot)"
    http_timeout_seconds: float = 30.0
    http_max_retries: int = 3
    http_backoff_base_seconds: float = 1.0
    rate_limit_per_host_seconds: float = 1.0
    respect_robots_txt: bool = True

    # SmartRecruiters: api.smartrecruiters.com/robots.txt disallows all crawlers except
    # LinkedInBot, so the connector reads the public career pages instead. Only switch
    # this on with written permission from SmartRecruiters (the API path still obeys
    # robots.txt, so it would just fail otherwise).
    smartrecruiters_use_api: bool = False

    # Generic sitemap + JSON-LD connectors (jobindex/connectors/generic_jsonld.py)
    # A crawl whose open-job count is more than this fraction below the last successful
    # crawl fails instead of closing the jobs (0.5 = "lost more than half"; 1 disables).
    generic_max_job_drop: float = 0.5
    # Job pages whose sitemap entry has no <lastmod> are re-fetched every N days
    # (staggered per URL), otherwise edits would never be noticed.
    generic_refresh_days: int = 7

    # Deduplication (jobindex/dedup): postings further apart than this are different
    # openings even with the same company, title and location.
    dedup_date_window_days: int = 30

    # Scheduler (`python -m jobindex.cli schedule`, see jobindex/scheduler)
    # Minutes between two crawls of a source; per-platform overrides as JSON, e.g.
    # CRAWL_PLATFORM_INTERVALS='{"workday": 240, "greenhouse": 30}'. Platforms with a
    # request per job page (sitemap / detail connectors) are slower and default higher.
    crawl_interval_minutes: int = 60
    crawl_platform_intervals: dict[str, int] = {
        "workday": 240,
        "oracle": 240,
        "successfactors": 240,
        "hireology": 720,  # one ~8 MB sitemap shared by all tenants
    }
    crawl_concurrency: int = 4  # crawls running at the same time
    crawl_jitter: float = 0.1  # up to +10% of the interval, so sources spread out
    # A `running` fetch run older than this is considered abandoned (process died).
    crawl_stale_after_hours: float = 12
    scheduler_tick_seconds: int = 60
    scheduler_heartbeat_file: str = "/tmp/jobindex-scheduler.heartbeat"

    # Size budget (docs/OPERATIONS.md#size-budget). Defaults keep everything; the hosted
    # demo (500 MB Supabase free tier) sets them in .github/workflows/crawl.yml.
    # At most this many postings per source (0 = no cap). The full list is still fetched
    # (completeness rules are unchanged), postings already stored are kept first, and a
    # posting is only closed when it is missing from the full list.
    max_jobs_per_source: int = 0
    # description_html longer than this is stored as "" (description_text is always
    # kept; the API falls back to it). None = keep all HTML, 0 = never store HTML.
    description_html_max_chars: int | None = None
    # job_changes old/new values are cut to this many characters (None = full values).
    job_change_value_max_chars: int | None = None
    # `prune`: delete closed postings closed more than N days ago (with their history),
    # job_changes older than N days, and fetch_runs older than N days (each source's
    # latest successful run is kept: the job-drop guard compares against it).
    # None = keep forever.
    closed_job_retention_days: int | None = None
    job_change_retention_days: int | None = None
    fetch_run_retention_days: int | None = None

    log_level: str = "INFO"

    @model_validator(mode="after")
    def _serverless_defaults(self) -> Self:
        if self.vercel:
            explicit = self.model_fields_set
            if "database_null_pool" not in explicit:
                self.database_null_pool = True
            if "database_read_only" not in explicit:
                self.database_read_only = True
            if "api_cache_seconds" not in explicit:
                self.api_cache_seconds = 60
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings. Tests call ``get_settings.cache_clear()``."""
    return Settings()
