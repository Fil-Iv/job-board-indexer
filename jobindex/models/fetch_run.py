from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from jobindex.models.base import Base, utcnow
from jobindex.models.enums import FetchStatus, enum_values


class FetchRun(Base):
    """One crawl attempt of one source. Failures are recorded here, never inferred."""

    __tablename__ = "fetch_runs"
    __table_args__ = (Index("ix_fetch_runs_source_id_started_at", "source_id", "started_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(Integer, ForeignKey("sources.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[FetchStatus] = mapped_column(
        Enum(
            FetchStatus,
            name="fetch_status",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=enum_values,
        ),
        default=FetchStatus.RUNNING,
    )
    jobs_found: Mapped[int] = mapped_column(Integer, default=0)
    jobs_new: Mapped[int] = mapped_column(Integer, default=0)
    jobs_updated: Mapped[int] = mapped_column(Integer, default=0)
    jobs_closed: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
