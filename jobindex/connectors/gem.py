"""Gem ATS job boards (``jobs.gem.com/{slug}``; public Greenhouse-style JSON API).

List: GET https://api.gem.com/job_board/v0/{slug}/job_posts/
      -> [{id, title, absolute_url, content (HTML), content_plain, location{name},
           location_type (in_office|hybrid|remote), employment_type, offices[{name,
           location{name}}], departments[], first_published_at, created_at, updated_at,
           requisition_id}]
      Every published post, with descriptions, in one response (verified 2026-11-07).
      Unknown slugs answer 404.

Board identifier: the board slug, e.g. ``nominal``.

URL formats recognised:
    jobs.gem.com/{slug}[/{post-uuid}]     api.gem.com/job_board/v0/{slug}/job_posts/
Custom-domain career pages: links/iframes to the above (generic HTML detection).
"""

import re
from typing import Any

from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.models.enums import RemoteType
from jobindex.normalize import normalize_employment_type, parse_datetime, split_url

API = "https://api.gem.com/job_board/v0"
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")
_RESERVED = {"api", "static", "assets", "embed"}
_LOCATION_TYPE = {
    "in_office": RemoteType.ONSITE,
    "onsite": RemoteType.ONSITE,
    "hybrid": RemoteType.HYBRID,
    "remote": RemoteType.REMOTE,
}


def _locations(post: dict[str, Any]) -> str | None:
    names = [(post.get("location") or {}).get("name")]
    names += [
        (o.get("location") or {}).get("name") or o.get("name") for o in post.get("offices") or []
    ]
    return "; ".join(dict.fromkeys(str(n) for n in names if n)) or None


@register
class GemConnector(BaseConnector):
    platform = "gem"
    company_name_sources = (re.compile(r"^(?P<name>.+?) Careers$"),)

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, segments, _ = split_url(url)
        lowered = [s.lower() for s in segments]
        if host == "jobs.gem.com":
            slug = lowered[0] if lowered else ""
        elif host == "api.gem.com" and lowered[:2] == ["job_board", "v0"] and len(lowered) > 2:
            slug = lowered[2]
        else:
            return None
        if slug in _RESERVED or not _SLUG.match(slug):
            return None
        return slug

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://jobs.gem.com/{board_identifier}"

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        data = await self.http.get_json(f"{API}/{board_identifier}/job_posts/")
        if not isinstance(data, list):
            raise ConnectorError("gem: response is not a JSON array")
        return [self._map(p) for p in data]

    @staticmethod
    def _map(post: dict[str, Any]) -> RawJob:
        if not post.get("id"):
            raise ConnectorError("gem: job post without 'id'")
        url = post.get("absolute_url") or None
        return RawJob(
            external_id=str(post["id"]),
            title=str(post.get("title") or "").strip(),
            description_html=post.get("content") or None,
            description_text=post.get("content_plain") or None,
            location_raw=_locations(post),
            remote_type=_LOCATION_TYPE.get(str(post.get("location_type")), RemoteType.UNKNOWN),
            employment_type=normalize_employment_type(post.get("employment_type")),
            posted_at=parse_datetime(post.get("first_published_at") or post.get("created_at")),
            source_url=url,
            apply_url=url,
        )
