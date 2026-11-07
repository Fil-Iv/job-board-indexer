"""Response models of the HTTP API (they also define the OpenAPI schema)."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from jobindex.models.enums import JobStatus, RemoteType


class StatusFilter(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    ANY = "any"


class EmploymentType(StrEnum):
    """Values produced by ``normalize.normalize_employment_type``."""

    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    TEMPORARY = "temporary"
    INTERNSHIP = "internship"
    APPRENTICESHIP = "apprenticeship"


class JobSourceOut(BaseModel):
    """One listing of an opening: the posting as one source (job board) publishes it."""

    job_id: int = Field(description="ID of this source's posting (`jobs.id`).")
    source_id: int
    platform: str = Field(examples=["greenhouse"])
    board_identifier: str = Field(examples=["acme"])
    status: JobStatus
    source_url: str | None = Field(description="The posting on the original site.")
    apply_url: str | None = Field(description="Where to apply, if the platform gives one.")
    first_seen_at: datetime
    last_seen_at: datetime


class JobOut(BaseModel):
    """One opening. Postings of the same opening on several sources are merged
    (`jobindex.dedup`); `sources` lists every listing."""

    id: int = Field(description="Canonical posting ID; use it with `GET /jobs/{id}`.")
    title: str
    company: str | None
    location_raw: str | None = Field(description="Location as the source wrote it.")
    city: str | None
    region: str | None
    country: str | None = Field(description="ISO 3166-1 alpha-2 code.", examples=["DE"])
    remote_type: RemoteType
    employment_type: str | None = Field(examples=["full_time"])
    salary_min: Decimal | None
    salary_max: Decimal | None
    salary_currency: str | None = Field(examples=["EUR"])
    salary_period: str | None = Field(examples=["year"])
    posted_at: datetime | None = Field(description="Publication date given by the source.")
    status: JobStatus = Field(description="`open` if at least one source still lists it.")
    first_seen_at: datetime = Field(description="When any source first listed it.")
    last_seen_at: datetime = Field(description="Last successful crawl that listed it.")
    closed_at: datetime | None
    sources: list[JobSourceOut]


class JobDetailOut(JobOut):
    description_html: str | None
    description_text: str | None


class JobSearchOut(BaseModel):
    total: int = Field(description="Number of matching openings (not postings).")
    page: int
    size: int
    pages: int
    items: list[JobOut]


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    platform: str
    board_identifier: str
    company_name: str | None
    base_url: str
    active: bool
    open_jobs: int = Field(description="Open postings currently stored for this source.")
    last_success_at: datetime | None
    last_failure_at: datetime | None
    consecutive_failures: int = Field(description="Failed crawls since the last success.")
    last_error: str | None


class PlatformStats(BaseModel):
    platform: str
    sources: int
    active_sources: int
    failing_sources: int = Field(description="Active sources whose last crawl failed.")
    open_jobs: int
    total_jobs: int
    last_success_at: datetime | None = Field(description="Most recent successful crawl.")
    last_failure_at: datetime | None


class FailureOut(BaseModel):
    fetch_run_id: int
    source_id: int
    platform: str
    board_identifier: str
    started_at: datetime
    finished_at: datetime | None
    error: str | None


class StatsOut(BaseModel):
    sources: int
    active_sources: int
    failing_sources: int
    open_jobs: int = Field(description="Open postings, counting each source separately.")
    open_openings: int = Field(description="Open openings after deduplication.")
    total_jobs: int
    running_crawls: int
    last_crawl_at: datetime | None = Field(description="Most recent finished crawl.")
    platforms: list[PlatformStats]
    recent_failures: list[FailureOut] = Field(description="The 20 latest failed crawls.")


class HealthOut(BaseModel):
    status: str = Field(examples=["ok"])
    database: str = Field(examples=["ok"])
