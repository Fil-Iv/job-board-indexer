from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from jobindex.models.enums import JobStatus, RemoteType


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_id: int
    external_id: str
    title: str
    company: str | None
    location_raw: str | None
    city: str | None
    region: str | None
    country: str | None
    remote_type: RemoteType
    employment_type: str | None
    salary_min: Decimal | None
    salary_max: Decimal | None
    salary_currency: str | None
    salary_period: str | None
    posted_at: datetime | None
    source_url: str | None
    apply_url: str | None
    status: JobStatus
    first_seen_at: datetime
    last_seen_at: datetime
    closed_at: datetime | None


class JobDetailOut(JobOut):
    description_html: str | None
    description_text: str | None


class JobSearchOut(BaseModel):
    total: int
    items: list[JobOut]


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    platform: str
    board_identifier: str
    company_name: str | None
    base_url: str
    active: bool
    last_success_at: datetime | None
    last_failure_at: datetime | None
    consecutive_failures: int
    last_error: str | None
