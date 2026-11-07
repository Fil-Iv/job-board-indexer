from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from jobindex.models.base import Base, utcnow
from jobindex.models.enums import ChangeCause, enum_values


class JobChange(Base):
    """Field-level history of a job (one row per changed field per crawl).

    ``cause`` separates real changes at the source from noise we create ourselves:
    filter on ``cause = 'upstream'`` for "what employers changed".
    """

    __tablename__ = "job_changes"
    __table_args__ = (Index("ix_job_changes_job_id_changed_at", "job_id", "changed_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("jobs.id", ondelete="CASCADE"))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    field: Mapped[str] = mapped_column(String(64))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    #: normalize.PARSER_VERSION that produced ``new_value`` (1 = before versioning existed).
    parser_version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    cause: Mapped[ChangeCause] = mapped_column(
        Enum(
            ChangeCause,
            name="change_cause",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=enum_values,
        ),
        default=ChangeCause.UPSTREAM,
        server_default=ChangeCause.UPSTREAM.value,
    )
