from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from jobindex.models.base import Base


class Source(Base):
    """One job board on one platform (e.g. the Greenhouse board of a single company)."""

    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("platform", "board_identifier"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(64))
    board_identifier: Mapped[str] = mapped_column(String(512))
    company_name: Mapped[str | None] = mapped_column(String(512))
    base_url: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    #: Progress of an unfinished, resumable list fetch (see ``jobindex.checkpoint``).
    #: NULL whenever no crawl is half-way through.
    crawl_checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
