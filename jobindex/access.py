"""Check whether, and how, we may read a URL — and what kind of job data it serves.

Used for platform research and for the ``blocked_by_site`` evidence list
(``coverage/blocked_by_site.yaml``). It never fetches a URL that robots.txt disallows:
it reports the deciding robots rule instead. Allowed URLs are fetched once, with the
normal polite client, and inspected for machine-readable job data.
"""

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from jobindex.http import FetchError, HttpClient
from jobindex.schemaorg import count_job_postings


@dataclass(slots=True)
class AccessCheck:
    url: str
    checked_at: str
    user_agent: str
    robots_url: str
    robots_http_status: int | None
    robots_allowed: bool
    robots_rule: str | None  # deciding rule, e.g. "Disallow: /"; None = no rule matched
    #: robots.txt could not be read (DNS/TLS/connection error or HTTP 5xx). RFC 9309 says to
    #: treat that as "disallow all", so nothing is fetched, but it is an outage, not a
    #: rule the site wrote: it must not be reported as "blocked by the site".
    robots_unreachable: bool = False
    method: str = "GET"
    http_status: int | None = None  # None when not fetched (robots) or unreachable
    final_url: str | None = None
    content_type: str | None = None
    size: int | None = None
    error: str | None = None
    signals: dict[str, Any] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        """The site itself refuses us: a robots.txt rule, or HTTP 401/403/429."""
        if self.robots_unreachable:
            return False
        return not self.robots_allowed or self.http_status in (401, 403, 429)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def inspect_body(text: str, content_type: str | None) -> dict[str, Any]:
    """Which job-data formats does this response contain?"""
    signals: dict[str, Any] = {}
    stripped = text.lstrip()
    if stripped[:1] in "[{":
        try:
            data = json.loads(stripped)
        except ValueError:
            pass
        else:
            signals["json"] = (
                f"list[{len(data)}]"
                if isinstance(data, list)
                else "keys:" + ",".join(list(data)[:8])
                if isinstance(data, dict)
                else type(data).__name__
            )
            return signals
    if stripped.startswith("<?xml") or "<rss" in stripped[:500] or "xml" in (content_type or ""):
        signals["xml_items"] = len(re.findall(r"<(item|entry|job|position|url)[\s>]", text))
        signals["sitemap"] = "<urlset" in text or "<sitemapindex" in text
        return signals
    signals["jobposting_jsonld"], signals["jobposting_microdata"] = count_job_postings(text)
    signals["next_data"] = "__NEXT_DATA__" in text
    signals["rss_link"] = bool(re.search(r'type="application/(rss|atom)\+xml"', text))
    return signals


async def check_access(
    url: str, http: HttpClient, *, method: str = "GET", json_body: Any = None
) -> AccessCheck:
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}".lower()
    allowed, rule = await http.robots_verdict(url)
    check = AccessCheck(
        url=url,
        checked_at=datetime.now(UTC).isoformat(timespec="seconds"),
        user_agent=http.user_agent,
        robots_url=f"{origin}/robots.txt",
        robots_http_status=http.robots_status.get(origin),
        robots_allowed=allowed,
        robots_rule=rule,
        method=method,
    )
    status = check.robots_http_status
    if not allowed and (status is None or status >= 500):
        check.robots_unreachable = True
        check.robots_rule = None  # the synthetic disallow-all is not the site's rule
        check.error = f"robots.txt unreachable ({'no response' if status is None else status})"
    if not allowed:
        return check  # never fetch what robots.txt disallows
    try:
        response = await http.request(method, url, json=json_body)
    except FetchError as exc:
        check.http_status = exc.status_code
        check.error = str(exc)
        return check
    check.http_status = response.status_code
    check.final_url = str(response.url)
    check.content_type = response.headers.get("content-type", "").split(";")[0] or None
    check.size = len(response.content)
    check.signals = inspect_body(response.text, check.content_type)
    return check
