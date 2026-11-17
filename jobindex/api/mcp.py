"""MCP server at ``/mcp``: job search as tools for AI agents (Model Context Protocol).

Transport: *Streamable HTTP* in its stateless JSON form. Every JSON-RPC message is one
``POST /mcp``; requests are answered with ``application/json``, notifications with
``202``. No sessions, no server-sent events (other methods get ``405``), so it runs on a
serverless function (Vercel) that keeps nothing between requests. Implemented here
directly (~200 lines) instead of with the MCP SDK, whose HTTP transport needs a
long-running task group that serverless runtimes do not provide.

Tools (read-only, no auth): ``search_jobs`` and ``get_job``, the same queries as
``GET /jobs`` and ``GET /jobs/{id}``. Requests are rate-limited per client IP
(``MCP_RATE_LIMIT_PER_MINUTE``); on Vercel the limit is per function instance.
"""

import json
from typing import Any, Literal

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from jobindex import __version__
from jobindex.api.deps import SessionDep
from jobindex.api.ratelimit import client_ip, limiter
from jobindex.api.schemas import JobDetailOut, JobOut
from jobindex.api.search import SearchParams, get_opening, search
from jobindex.config import get_settings
from jobindex.currency import infer_currency
from jobindex.models import RemoteType

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")  # newest first
RESULTS_PER_PAGE = 10
MAX_PAGE = 50
MAX_DESCRIPTION_CHARS = 20_000
MAX_BODY_BYTES = 64_000

INSTRUCTIONS = (
    "JobForge indexes open job postings from many ATS and job-board platforms. "
    "Use search_jobs to find openings (full-text query plus filters), then get_job for "
    "the full description and apply links. Results are deduplicated openings, newest "
    "first, or by relevance when q is given."
)

# JSON-RPC error codes
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602
RATE_LIMITED = -32000


class SearchJobsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: str | None = Field(
        None,
        max_length=500,
        description='Full-text query, web-search syntax: python "data engineer" -senior',
    )
    company: str | None = Field(None, max_length=200, description="Company name contains.")
    location: str | None = Field(
        None,
        max_length=200,
        description="City, region or country (name or ISO code), comma-separated to combine,"
        " e.g. 'Berlin, Germany' or 'Austin, TX'.",
    )
    remote_type: Literal["remote", "hybrid", "onsite"] | None = Field(
        None, description="Work arrangement."
    )
    seniority: Literal["internship", "junior", "mid", "senior", "lead", "management"] | None = (
        Field(None, description="Level derived from the job title.")
    )
    page: int = Field(1, ge=1, le=MAX_PAGE, description=f"1-based page, {RESULTS_PER_PAGE}/page.")


class GetJobArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=1, description="Opening ID from search_jobs.")


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    return schema


TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_jobs",
        "title": "Search jobs",
        "description": "Search open job postings. Returns openings with id, title, company,"
        " location, work arrangement, seniority, salary (when published), posting date"
        " and URL.",
        "inputSchema": _schema(SearchJobsArgs),
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "get_job",
        "title": "Get job",
        "description": "One opening by id: full description text and every listing's"
        " posting and apply URL.",
        "inputSchema": _schema(GetJobArgs),
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]


def _summary(job: JobOut) -> dict[str, Any]:
    main = job.sources[0] if job.sources else None
    salary = None
    if job.salary_min is not None or job.salary_max is not None:
        salary = {
            "min": float(job.salary_min) if job.salary_min is not None else None,
            "max": float(job.salary_max) if job.salary_max is not None else None,
            # Not published by the source: the location's currency, flagged as inferred.
            "currency": job.salary_currency or infer_currency(job.country),
            "currency_inferred": job.salary_currency is None,
            "period": job.salary_period,
        }
    return {
        "id": job.id,
        "title": job.title,
        "company": job.company,
        "location": ", ".join(p for p in (job.city, job.region, job.country) if p)
        or job.location_raw,
        "remote_type": job.remote_type.value,
        "seniority": job.seniority.value,
        "employment_type": job.employment_type,
        "salary": salary,
        "posted": (job.posted_at or job.first_seen_at).date().isoformat(),
        "url": (main.source_url or main.apply_url) if main else None,
    }


async def search_jobs(session: AsyncSession, args: SearchJobsArgs) -> dict[str, Any]:
    result = await search(
        session,
        SearchParams(
            q=(args.q or "").strip() or None,
            company=(args.company or "").strip() or None,
            location=(args.location or "").strip() or None,
            remote_type=RemoteType(args.remote_type) if args.remote_type else None,
            seniority=args.seniority,
            page=args.page,
            size=RESULTS_PER_PAGE,
        ),
    )
    return {
        "total": result.total,
        "page": result.page,
        "pages": min(result.pages, MAX_PAGE),
        "jobs": [_summary(job) for job in result.items],
    }


async def get_job(session: AsyncSession, args: GetJobArgs) -> dict[str, Any] | None:
    opening: JobDetailOut | None = await get_opening(session, args.id)
    if opening is None:
        return None
    text = opening.description_text or ""
    if len(text) > MAX_DESCRIPTION_CHARS:
        text = text[:MAX_DESCRIPTION_CHARS] + " …"
    return {
        **_summary(opening),
        "status": opening.status.value,
        "description": text,
        "listings": [
            {
                "platform": s.platform,
                "status": s.status.value,
                "posting_url": s.source_url,
                "apply_url": s.apply_url,
            }
            for s in opening.sources
        ],
    }


def _error(msg_id: Any, code: int, message: str, status: int = 200) -> JSONResponse:
    body = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
    return JSONResponse(body, status_code=status)


def _result(msg_id: Any, result: dict[str, Any]) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _tool_result(data: dict[str, Any] | None, error: str | None = None) -> dict[str, Any]:
    if error is not None:
        return {"content": [{"type": "text", "text": error}], "isError": True}
    text = json.dumps(data, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}], "structuredContent": data}


async def call_tool(session: AsyncSession, params: dict[str, Any]) -> dict[str, Any]:
    name, arguments = params.get("name"), params.get("arguments") or {}
    try:
        if name == "search_jobs":
            found = await search_jobs(session, SearchJobsArgs.model_validate(arguments))
            return _tool_result(found)
        if name == "get_job":
            job = await get_job(session, GetJobArgs.model_validate(arguments))
            return _tool_result(job) if job else _tool_result(None, "No job with this id.")
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in exc.errors()
        )
        return _tool_result(None, f"Invalid arguments: {problems}")
    raise LookupError(name)


router = APIRouter(include_in_schema=False)


@router.post("/mcp")
async def mcp_endpoint(request: Request, session: SessionDep) -> Response:
    wait = limiter.retry_after(client_ip(request), get_settings().mcp_rate_limit_per_minute)
    if wait:
        response = _error(None, RATE_LIMITED, f"Rate limit exceeded, retry in {wait}s", 429)
        response.headers["Retry-After"] = str(wait)
        return response

    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return _error(None, INVALID_REQUEST, "Request too large", 413)
    try:
        message = json.loads(raw)
    except ValueError:
        return _error(None, PARSE_ERROR, "Parse error", 400)
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, INVALID_REQUEST, "Expected one JSON-RPC 2.0 message", 400)

    method = message.get("method")
    if "id" not in message:  # notification (e.g. notifications/initialized) or a response
        return Response(status_code=202)
    msg_id = message["id"]
    params = message.get("params") or {}
    if not isinstance(method, str) or not isinstance(params, dict):
        return _error(msg_id, INVALID_REQUEST, "Invalid request", 400)

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _result(
            msg_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "jobforge", "title": "JobForge", "version": __version__},
                "instructions": INSTRUCTIONS,
            },
        )
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        try:
            return _result(msg_id, await call_tool(session, params))
        except LookupError:
            return _error(msg_id, INVALID_PARAMS, f"Unknown tool: {params.get('name')}")
    return _error(msg_id, METHOD_NOT_FOUND, f"Method not found: {method}")
