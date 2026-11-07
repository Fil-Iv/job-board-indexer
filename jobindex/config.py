"""Application settings, read from environment variables (and an optional .env file)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://jobindex:jobindex@localhost:5432/jobindex"

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

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings. Tests call ``get_settings.cache_clear()``."""
    return Settings()
