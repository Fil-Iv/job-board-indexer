"""Check whether, and how, we may read a URL — and what kind of job data it serves.

Used for platform research and for the ``blocked_by_site`` evidence list
(``coverage/blocked_by_site.yaml``). It never fetches a URL that robots.txt disallows:
it reports the deciding robots rule instead. Allowed URLs are fetched once, with the
normal polite client, and inspected for machine-readable job data.

Redirects get the same rule as ``jobindex.http``: every hop is checked against the
robots.txt of its own host before it is fetched. Each hop's verdict is recorded in
``hops``; if a hop is disallowed, the check reports *that* hop's robots.txt and rule
(and counts as blocked), not the verdict for the URL we started from.
"""

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from jobindex.http import FetchError, HttpClient, RobotsDisallowedError
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
    #: Every URL requested, in order (the start URL, then each redirect target), with its
    #: robots verdict: {"url", "status", "robots_url", "robots_allowed", "robots_rule"}.
    #: "status" is None for a hop that was never fetched.
    hops: list[dict[str, Any]] = field(default_factory=list)

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


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


async def _hop(url: str, http: HttpClient, status: int | None) -> dict[str, Any]:
    allowed, rule = await http.robots_verdict(url)  # cached: the client already checked it
    return {
        "url": url,
        "status": status,
        "robots_url": f"{_origin(url)}/robots.txt",
        "robots_allowed": allowed,
        "robots_rule": rule,
    }


def _apply_verdict(check: AccessCheck, http: HttpClient, url: str) -> None:
    """Report the robots verdict that decides ``check``: the one for ``url``."""
    hop = next(h for h in check.hops if h["url"] == url)
    origin = _origin(url)
    check.robots_url = hop["robots_url"]
    check.robots_http_status = http.robots_status.get(origin)
    check.robots_allowed = hop["robots_allowed"]
    check.robots_rule = hop["robots_rule"]
    status = check.robots_http_status
    if not check.robots_allowed and (status is None or status >= 500):
        check.robots_unreachable = True
        check.robots_rule = None  # the synthetic disallow-all is not the site's rule
        check.error = f"robots.txt unreachable ({'no response' if status is None else status})"


async def check_access(
    url: str, http: HttpClient, *, method: str = "GET", json_body: Any = None
) -> AccessCheck:
    check = AccessCheck(
        url=url,
        checked_at=datetime.now(UTC).isoformat(timespec="seconds"),
        user_agent=http.user_agent,
        robots_url="",
        robots_http_status=None,
        robots_allowed=False,
        robots_rule=None,
        method=method,
    )
    check.hops.append(await _hop(url, http, None))
    _apply_verdict(check, http, url)
    if not check.robots_allowed:
        return check  # never fetch what robots.txt disallows
    try:
        response = await http.request(method, url, json=json_body)
    except RobotsDisallowedError as exc:
        # A redirect led to a URL its own host's robots.txt disallows (it was not fetched).
        # Record the hops that were fetched, then report the disallowed hop's verdict.
        check.hops = [await _hop(str(r.url), http, r.status_code) for r in exc.history]
        check.hops.append(await _hop(exc.url, http, None))
        _apply_verdict(check, http, exc.url)
        check.error = check.error or f"redirected to {exc.url}, which robots.txt disallows"
        return check
    except FetchError as exc:
        check.hops = [await _hop(str(r.url), http, r.status_code) for r in exc.history]
        check.hops.append(await _hop(exc.url, http, exc.status_code))
        check.http_status = exc.status_code
        check.error = str(exc)
        return check
    chain = [*response.history, response]
    check.hops = [await _hop(str(r.url), http, r.status_code) for r in chain]
    check.http_status = response.status_code
    check.final_url = str(response.url)
    check.content_type = response.headers.get("content-type", "").split(";")[0] or None
    check.size = len(response.content)
    check.signals = inspect_body(response.text, check.content_type)
    return check
