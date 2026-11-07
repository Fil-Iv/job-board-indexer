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

    # Scheduler
    scheduler_enabled: bool = False
    crawl_interval_minutes: int = 60
    crawl_concurrency: int = 4

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings. Tests call ``get_settings.cache_clear()``."""
    return Settings()
