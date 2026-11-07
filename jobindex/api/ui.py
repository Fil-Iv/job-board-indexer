"""Server-rendered search page at ``/`` (Jinja2, no build step).

It runs the same search as ``GET /jobs``. Form values are parsed leniently: an empty or
invalid field is ignored instead of producing a 422 error page.
"""

from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from jobindex.api.deps import SessionDep
from jobindex.api.schemas import EmploymentType, StatusFilter
from jobindex.api.search import SearchParams, search
from jobindex.models import RemoteType

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

PAGE_SIZE = 20


def safe_url(url: str | None) -> str | None:
    """Only http(s) links from crawled data reach an ``href`` (no ``javascript:``)."""
    if url and urlsplit(url).scheme.lower() in {"http", "https"}:
        return url
    return None


templates.env.filters["safe_url"] = safe_url


def _enum[E: (RemoteType, EmploymentType, StatusFilter)](
    cls: type[E], value: str | None, default: E | None = None
) -> E | None:
    try:
        return cls(value) if value else default
    except ValueError:
        return default


def _decimal(value: str | None) -> Decimal | None:
    try:
        number = Decimal(value.replace(",", "").strip()) if value else None
    except InvalidOperation:
        return None
    return number if number is not None and number.is_finite() and number >= 0 else None


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _page(value: str | None) -> int:
    return int(value) if value and value.isdigit() and 0 < int(value) < 10_000 else 1


def form_params(request: Request) -> SearchParams:
    qp = request.query_params
    employment = _enum(EmploymentType, qp.get("employment_type"))
    return SearchParams(
        q=(qp.get("q") or "").strip() or None,
        company=(qp.get("company") or "").strip() or None,
        location=(qp.get("location") or "").strip() or None,
        remote_type=_enum(RemoteType, qp.get("remote_type")),
        employment_type=employment.value if employment else None,
        salary_min=_decimal(qp.get("salary_min")),
        posted_since=_date(qp.get("posted_since")),
        status=_enum(StatusFilter, qp.get("status"), StatusFilter.OPEN) or StatusFilter.OPEN,
        page=_page(qp.get("page")),
        size=PAGE_SIZE,
    )


router = APIRouter(include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    session: SessionDep,
    params: Annotated[SearchParams, Depends(form_params)],
) -> HTMLResponse:
    results = await search(session, params)
    form = {k: v for k, v in request.query_params.items() if k != "page" and v}

    def page_url(page: int) -> str:
        return "/?" + urlencode({**form, "page": page})

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "form": form,
            "params": params,
            "results": results,
            "page_url": page_url,
            "remote_types": [r.value for r in RemoteType if r is not RemoteType.UNKNOWN],
            "employment_types": [e.value for e in EmploymentType],
        },
    )
