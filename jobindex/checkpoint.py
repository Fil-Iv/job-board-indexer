"""Resumable fetches: a small per-source key/value checkpoint a connector may use.

Long list crawls (Workday: thousands of postings at 20 per request, one request per second)
save their progress here; if the crawl fails half-way, the next attempt continues from the
checkpoint instead of from page 1. In production the checkpoint lives in
``sources.crawl_checkpoint`` (see :class:`SourceCheckpoint`); tests and ad-hoc runs use
:class:`MemoryCheckpoint`.

Rules for connectors:

* A checkpoint only ever holds an *unfinished* list. Clear it as soon as the list is
  complete, so a later failure (e.g. while fetching details) never makes the next crawl
  reuse an old, finished list.
* Treat a loaded checkpoint as a hint: validate it (format version, board, age) and still
  check the final result for completeness. ``fetch_jobs`` must return the complete list or
  raise, resumed or not.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from jobindex.models import Source


class CheckpointStore(Protocol):
    async def load(self) -> dict[str, Any] | None: ...

    async def save(self, data: dict[str, Any]) -> None: ...

    async def clear(self) -> None: ...


class MemoryCheckpoint:
    """In-process checkpoint (default for connectors created outside the crawler)."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = data
        self.saves = 0

    async def load(self) -> dict[str, Any] | None:
        return self.data

    async def save(self, data: dict[str, Any]) -> None:
        self.data = data
        self.saves += 1

    async def clear(self) -> None:
        self.data = None


class SourceCheckpoint:
    """Checkpoint stored in ``sources.crawl_checkpoint``; every save commits, so it survives
    the crawl failing (the crawler rolls back only its own uncommitted work)."""

    def __init__(self, session: AsyncSession, source: Source) -> None:
        self._session = session
        self._source = source

    async def load(self) -> dict[str, Any] | None:
        return self._source.crawl_checkpoint

    async def save(self, data: dict[str, Any]) -> None:
        self._source.crawl_checkpoint = dict(data)  # new object: JSONB change detection
        await self._session.commit()

    async def clear(self) -> None:
        if self._source.crawl_checkpoint is not None:
            self._source.crawl_checkpoint = None
            await self._session.commit()


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def is_fresh(saved_at: Any, max_age: timedelta) -> bool:
    """True if ``saved_at`` (ISO timestamp written by :func:`now_iso`) is recent enough."""
    try:
        saved = datetime.fromisoformat(str(saved_at))
    except ValueError:
        return False
    return saved.tzinfo is not None and datetime.now(UTC) - saved <= max_age
