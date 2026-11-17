"""Adding career pages: check a URL, queue it for review, or save it as a source.

Used by ``/add`` (jobindex/api/add_page.py), ``POST /sources`` and the
``check-pending`` CLI command.

* ``check_url`` detects the platform. Where live checks are allowed
  (``INTAKE_LIVE_CHECKS``; off on Vercel, which must not crawl) it also reads robots.txt,
  inspects the page when the URL has no platform pattern, and dry-runs the connector's
  ``fetch_jobs`` for a job count. Everything goes through ``HttpClient`` (robots.txt,
  User-Agent, rate limit). Without live checks only the URL pattern is used.
* ``queue`` stores the URL in ``pending_sources`` (one row per URL, a global hourly cap
  so the public demo can't be flooded). The website's database role may only insert there.
* ``save_source`` registers the source (needs ``ADMIN_TOKEN`` in the web UI / API).
* ``process_pending`` runs the live check for queued URLs (crawl workflow).
"""

import asyncio
import hmac
import logging
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.access import check_access
from jobindex.catalog import platform_name
from jobindex.config import get_settings
from jobindex.connectors.registry import Registry, ResolvedBoard
from jobindex.http import FetchError, HttpClient, RobotsDisallowedError
from jobindex.models import Job, JobStatus, PendingSource, Source
from jobindex.models.base import utcnow

log = logging.getLogger(__name__)
MAX_URL_LENGTH = 2048


class IntakeError(ValueError):
    """The URL can't be checked or queued (message is shown to the user)."""


class QueueFull(IntakeError):
    """Too many requests right now (HTTP 429)."""


def clean_url(raw: str | None) -> str:
    """A plain http(s) URL, or ``IntakeError``. "acme.com/careers" gets "https://"."""
    url = (raw or "").strip()
    if not url:
        raise IntakeError("Paste the URL of a career page or job board.")
    if "://" not in url:
        url = "https://" + url
    if len(url) > MAX_URL_LENGTH:
        raise IntakeError("That URL is too long.")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or "." not in parts.hostname:
        raise IntakeError("That doesn't look like a web address (http or https).")
    if parts.username or parts.password:
        raise IntakeError("URLs with a user name or password are not accepted.")
    return url


def token_ok(given: str | None) -> bool:
    """``given`` equals ``ADMIN_TOKEN`` (constant-time); False when no token is set."""
    expected = get_settings().admin_token
    return bool(expected and given) and hmac.compare_digest(
        (given or "").encode(), expected.encode() if expected else b""
    )


@dataclass(slots=True)
class Check:
    url: str
    platform: str | None = None
    board_identifier: str | None = None
    detected_by: str | None = None  # "url" pattern, "page" (HTML / redirect)
    live: bool = False  # robots.txt / page / dry-run were checked
    robots_allowed: bool | None = None
    robots_rule: str | None = None
    preview_jobs: int | None = None
    error: str | None = None
    existing_source_id: int | None = None
    existing_open_jobs: int | None = None

    @property
    def platform_name(self) -> str:
        return platform_name(self.platform)

    @property
    def found(self) -> bool:
        return self.platform is not None


async def check_url(
    session: AsyncSession, registry: Registry, url: str, http: HttpClient | None
) -> Check:
    """Detect the platform (and, with ``http``, check robots.txt and dry-run the list)."""
    check = Check(url=url)
    board = registry.detect(url)
    if board is not None:
        check.detected_by = "url"
    if http is not None:
        check.live = True
        try:
            access = await check_access(url, http)
            if access.robots_unreachable:  # an outage, not a rule: unknown, nothing fetched
                check.error = f"The site could not be reached: {access.error or 'no response'}"
                http = None
            else:
                check.robots_allowed = access.robots_allowed
                check.robots_rule = access.robots_rule
            if board is None and http is not None and access.robots_allowed:
                board = await registry.resolve_url(url, http)
                check.detected_by = "page" if board else None
        except FetchError as exc:
            check.error = f"Could not load the page: {exc}"
    if board is not None:
        check.platform, check.board_identifier = board.platform, board.board_identifier
        if http is not None and check.robots_allowed is not False:
            await _dry_run(check, registry, board, http)
        await _existing(session, check)
    return check


async def _dry_run(
    check: Check, registry: Registry, board: ResolvedBoard, http: HttpClient
) -> None:
    connector = registry.get(board.platform)(http)
    timeout = get_settings().intake_dry_run_timeout_seconds
    try:
        jobs = await asyncio.wait_for(connector.fetch_jobs(board.board_identifier), timeout)
    except RobotsDisallowedError as exc:
        check.robots_allowed = False
        check.error = f"robots.txt disallows the job list: {exc}"
    except TimeoutError:
        check.error = (
            f"The dry run took longer than {timeout:.0f} s (a large board);"
            " the crawler will fetch it in full."
        )
    except Exception as exc:  # any connector failure is reported, not raised
        check.error = f"Dry run failed: {type(exc).__name__}: {exc}"
    else:
        check.preview_jobs = len({job.external_id for job in jobs})


async def _existing(session: AsyncSession, check: Check) -> None:
    source = await session.scalar(
        select(Source).where(
            Source.platform == check.platform, Source.board_identifier == check.board_identifier
        )
    )
    if source is not None:
        check.existing_source_id = source.id
        check.existing_open_jobs = (
            await session.scalar(
                select(func.count()).where(Job.source_id == source.id, Job.status == JobStatus.OPEN)
            )
            or 0
        )


async def queue(session: AsyncSession, check: Check) -> tuple[PendingSource, bool]:
    """Store the URL for review. Returns (row, created); an already queued URL is
    returned as is. Raises ``QueueFull`` beyond the hourly / total caps."""
    existing = await session.scalar(select(PendingSource).where(PendingSource.url == check.url))
    if existing is not None:
        return existing, False
    settings = get_settings()
    now = utcnow()
    recent = await session.scalar(
        select(func.count()).where(PendingSource.submitted_at > now - timedelta(hours=1))
    )
    waiting = await session.scalar(select(func.count()).where(PendingSource.status == "pending"))
    if (recent or 0) >= settings.intake_queue_per_hour or (
        waiting or 0
    ) >= settings.intake_queue_max_pending:
        raise QueueFull("The review queue is full right now; please try again later.")
    row = PendingSource(
        url=check.url,
        submitted_at=now,
        status="pending",
        platform=check.platform,
        board_identifier=check.board_identifier,
    )
    session.add(row)
    await session.commit()
    return row, True


async def save_source(
    session: AsyncSession, registry: Registry, check: Check
) -> tuple[Source, bool]:
    """Register the detected board as a source (or return the existing one)."""
    if check.platform is None or check.board_identifier is None:
        raise IntakeError("No supported platform was found for this URL.")
    if check.robots_allowed is False:
        raise IntakeError("robots.txt disallows this site for our crawler; it is not added.")
    existing = await session.scalar(
        select(Source).where(
            Source.platform == check.platform, Source.board_identifier == check.board_identifier
        )
    )
    if existing is not None:
        return existing, False
    connector = registry.get(check.platform)
    source = Source(
        platform=check.platform,
        board_identifier=check.board_identifier,
        base_url=connector.board_url(check.board_identifier),
        active=True,
    )
    session.add(source)
    await session.flush()
    pending = await session.scalar(select(PendingSource).where(PendingSource.url == check.url))
    if pending is not None:
        pending.status, pending.source_id = "added", source.id
    await session.commit()
    return source, True


async def process_pending(
    session_factory: async_sessionmaker[AsyncSession],
    registry: Registry,
    http: HttpClient,
    limit: int = 20,
) -> int:
    """Live-check queued URLs (robots.txt, platform, dry-run count). Returns how many."""
    async with session_factory() as session:
        rows = list(
            await session.scalars(
                select(PendingSource)
                .where(PendingSource.status == "pending")
                .order_by(PendingSource.submitted_at)
                .limit(limit)
            )
        )
        for row in rows:
            check = await check_url(session, registry, row.url, http)
            row.status = "checked"
            row.checked_at = utcnow()
            row.platform, row.board_identifier = check.platform, check.board_identifier
            row.robots_allowed = check.robots_allowed
            row.preview_jobs = check.preview_jobs
            row.error = check.error or (None if check.found else "no supported platform found")
            row.source_id = check.existing_source_id
            await session.commit()
            log.info(
                "pending %s %s: %s/%s robots=%s jobs=%s %s",
                row.id,
                row.url,
                row.platform,
                row.board_identifier,
                row.robots_allowed,
                row.preview_jobs,
                row.error or "",
            )
        return len(rows)
