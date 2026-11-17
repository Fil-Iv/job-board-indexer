"""Add a career page: the ``/add`` page and ``POST /sources`` (see jobindex/intake.py).

Anyone can check a URL and request indexing (stored in ``pending_sources`` for review).
Saving a source directly needs ``ADMIN_TOKEN`` and a writable database, so it is off on
the read-only Vercel deployment. Both endpoints are rate-limited per client.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from urllib.parse import parse_qs

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from jobindex.api.deps import SessionDep, session_factory
from jobindex.api.ratelimit import RateLimiter, client_ip
from jobindex.api.ui import header_stats, templates
from jobindex.config import get_settings
from jobindex.connectors.registry import default_registry
from jobindex.http import HttpClient
from jobindex.intake import (
    Check,
    IntakeError,
    QueueFull,
    check_url,
    clean_url,
    queue,
    save_source,
    token_ok,
)
from jobindex.models import PendingSource

CHECKS_PER_MINUTE = 10
WRITES_PER_10_MINUTES = 5
check_limiter = RateLimiter(window=60)
write_limiter = RateLimiter(window=600)


@asynccontextmanager
async def writable_session(request: Request) -> AsyncIterator[AsyncSession]:
    """A session whose transaction may write (the Vercel engine defaults to READ ONLY;
    the database role still limits it to INSERT on pending_sources)."""
    async with session_factory(request.app)() as session:
        await session.connection(execution_options={"postgresql_readonly": False})
        yield session


async def _check(session: AsyncSession, url: str) -> Check:
    if not get_settings().intake_live_checks:
        return await check_url(session, default_registry(), url, None)
    async with HttpClient.from_settings() as http:
        return await check_url(session, default_registry(), url, http)


def can_save() -> bool:
    settings = get_settings()
    return bool(settings.admin_token) and not settings.database_read_only


def _limited(limiter: RateLimiter, request: Request, limit: int) -> int:
    return limiter.retry_after(client_ip(request), limit)


router = APIRouter()


# --------------------------------------------------------------------------- web page


async def _page(
    request: Request, session: AsyncSession, status_code: int = 200, **context: object
) -> HTMLResponse:
    settings = get_settings()
    response = templates.TemplateResponse(
        request,
        "add.html",
        {
            "nav": "add",
            "stats": await header_stats(session),
            "live_checks": settings.intake_live_checks,
            "can_save": can_save(),
            **context,
        },
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/add", response_class=HTMLResponse, include_in_schema=False)
async def add_page(
    request: Request, session: SessionDep, request_id: int | None = None
) -> HTMLResponse:
    pending = await session.get(PendingSource, request_id) if request_id else None
    return await _page(request, session, pending=pending)


@router.post("/add", response_class=HTMLResponse, include_in_schema=False)
async def add_submit(request: Request, session: SessionDep) -> HTMLResponse:
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode("utf-8", "replace")).items()}
    action = form.get("action", "check")
    raw_url = form.get("url", "")
    limiter, limit = (
        (check_limiter, CHECKS_PER_MINUTE)
        if action == "check"
        else (write_limiter, WRITES_PER_10_MINUTES)
    )
    if wait := _limited(limiter, request, limit):
        response = await _page(
            request, session, 429, url=raw_url, error=f"Too many requests; try again in {wait} s."
        )
        response.headers["Retry-After"] = str(wait)
        return response
    try:
        url = clean_url(raw_url)
        check = await _check(session, url)
        if action == "request":
            async with writable_session(request) as writer:
                row, created = await queue(writer, check)
            return await _page(request, session, url=url, check=check, queued=row, created=created)
        if action == "save":
            if not can_save() or not token_ok(form.get("token")):
                return await _page(
                    request,
                    session,
                    403,
                    url=url,
                    check=check,
                    error="Saving needs the admin token (and a writable deployment).",
                )
            async with writable_session(request) as writer:
                source, created = await save_source(writer, default_registry(), check)
            return await _page(
                request, session, url=url, check=check, saved=source, created=created
            )
        return await _page(request, session, url=url, check=check)
    except QueueFull as exc:
        return await _page(request, session, 429, url=raw_url, error=str(exc))
    except IntakeError as exc:
        return await _page(request, session, 400, url=raw_url, error=str(exc))


# --------------------------------------------------------------------------- JSON API


class SourceRequestIn(BaseModel):
    url: str = Field(
        description="Career page or job board URL (hosted board, embed or custom domain).",
        examples=["https://jobs.lever.co/acme"],
    )
    save: bool = Field(
        default=False,
        description="Register the source now (needs `Authorization: Bearer <ADMIN_TOKEN>`"
        " and a writable deployment). Otherwise the URL is queued for review.",
    )


class SourceRequestOut(BaseModel):
    status: Literal["created", "exists", "queued", "already_queued"] = Field(
        description="`created`/`exists`: a source (saved with the admin token). "
        "`queued`/`already_queued`: stored in the review queue."
    )
    url: str
    platform: str | None = Field(description="Detected platform key, if any.")
    platform_name: str | None
    board_identifier: str | None
    detected_by: Literal["url", "page"] | None = Field(
        description="`url`: URL pattern; `page`: the page's HTML or redirect (live check)."
    )
    live_checked: bool = Field(
        description="robots.txt, page and dry run were checked here. `false` on the read-only "
        "demo, where the crawler checks queued URLs later."
    )
    robots_allowed: bool | None
    robots_rule: str | None
    preview_jobs: int | None = Field(description="Open jobs found by the dry-run fetch.")
    error: str | None
    source_id: int | None = Field(description="The source, when it exists or was created.")
    pending_id: int | None = Field(description="The review-queue entry, when queued.")


def _out(
    check: Check, status: str, source_id: int | None = None, pending_id: int | None = None
) -> dict[str, object]:
    return {
        "status": status,
        "url": check.url,
        "platform": check.platform,
        "platform_name": check.platform_name or None,
        "board_identifier": check.board_identifier,
        "detected_by": check.detected_by,
        "live_checked": check.live,
        "robots_allowed": check.robots_allowed,
        "robots_rule": check.robots_rule,
        "preview_jobs": check.preview_jobs,
        "error": check.error,
        "source_id": source_id if source_id is not None else check.existing_source_id,
        "pending_id": pending_id,
    }


@router.post(
    "/sources",
    tags=["sources"],
    summary="Add a career page",
    response_model=SourceRequestOut,
    status_code=202,
    responses={
        201: {
            "description": "Source created (admin token, `save: true`).",
            "model": SourceRequestOut,
        },
        200: {"description": "The source already exists.", "model": SourceRequestOut},
        400: {"description": "Not a usable http(s) URL, or robots.txt disallows the site."},
        403: {
            "description": "`save: true` without a valid admin token or on a read-only deployment."
        },
        429: {"description": "Rate limit or review-queue cap reached; see `Retry-After`."},
    },
)
async def post_source(
    body: SourceRequestIn,
    request: Request,
    response: Response,
    session: SessionDep,
    authorization: Annotated[
        str | None, Header(description="`Bearer <ADMIN_TOKEN>`, only for `save`.")
    ] = None,
) -> SourceRequestOut | JSONResponse:
    """Detect the platform of a career page and queue it for indexing (202), or, with
    the admin token and `save: true`, register it as a source right away (201).

    Detection uses the URL pattern; where crawling is allowed it also reads robots.txt,
    inspects the page (custom-domain career sites) and dry-runs the job list for a
    count. The public demo is read-only: it queues every request, and the crawler
    checks queued URLs on its next run. Rate-limited per client and in total."""
    limit = WRITES_PER_10_MINUTES
    if wait := _limited(write_limiter, request, limit):
        return JSONResponse(
            {"detail": f"rate limit exceeded, retry in {wait}s"},
            status_code=429,
            headers={"Retry-After": str(wait)},
        )
    try:
        url = clean_url(body.url)
        check = await _check(session, url)
        if body.save:
            token = (authorization or "").removeprefix("Bearer ").strip()
            if not can_save() or not token_ok(token):
                return JSONResponse(
                    {"detail": "save needs a valid admin token on a writable deployment"},
                    status_code=403,
                )
            async with writable_session(request) as writer:
                source, created = await save_source(writer, default_registry(), check)
            response.status_code = 201 if created else 200
            return SourceRequestOut.model_validate(
                _out(check, "created" if created else "exists", source_id=source.id)
            )
        async with writable_session(request) as writer:
            row, created = await queue(writer, check)
        return SourceRequestOut.model_validate(
            _out(check, "queued" if created else "already_queued", pending_id=row.id)
        )
    except QueueFull as exc:
        return JSONResponse({"detail": str(exc)}, status_code=429, headers={"Retry-After": "3600"})
    except IntakeError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
