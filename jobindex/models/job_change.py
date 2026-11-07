from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from jobindex.models.base import Base, utcnow


class JobChange(Base):
    """Field-level history of a job (one row per changed field per crawl)."""

    __tablename__ = "job_changes"
    __table_args__ = (Index("ix_job_changes_job_id_changed_at", "job_id", "changed_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("jobs.id", ondelete="CASCADE"))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    field: Mapped[str] = mapped_column(String(64))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
