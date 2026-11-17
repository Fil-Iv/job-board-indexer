from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from jobindex.models.base import Base, utcnow

PENDING_STATUSES = ("pending", "checked", "added", "rejected")


class PendingSource(Base):
    """A career page someone asked us to index (``/add`` "Request indexing" or an
    unauthenticated ``POST /sources``), waiting for review.

    The public website may only insert here (owner approval 2026-11-17, docs/DEPLOY.md):
    it detects the platform from the URL alone. ``python -m jobindex.cli check-pending``
    (run by the crawl workflow) does the network part: robots.txt, platform detection
    from the page, a dry-run job count. A maintainer then adds the source with
    ``add-source`` (status ``added``) or rejects it.
    """

    __tablename__ = "pending_sources"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'checked', 'added', 'rejected')",
            name="ck_pending_sources_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    url: Mapped[str] = mapped_column(Text, unique=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    #: Detected platform (from the URL when submitted, from the page after the check).
    platform: Mapped[str | None] = mapped_column(String(64))
    board_identifier: Mapped[str | None] = mapped_column(String(512))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    robots_allowed: Mapped[bool | None] = mapped_column(Boolean)
    preview_jobs: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[int | None] = mapped_column(Integer, server_default=text("NULL"))
