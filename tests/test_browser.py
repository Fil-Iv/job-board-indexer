"""jobindex.browser: the pure parts (no Chromium needed) and HttpClient.admit."""

from typing import Any, cast

import httpx
import pytest

from jobindex.browser import (
    CHALLENGE_PATTERNS,
    CHALLENGE_URLS,
    BrowserBlockedError,
    BrowserPage,
    browser_user_agent,
    host_allowed,
)
from tests.fakes import mock_http


def test_browser_user_agent_still_names_the_bot() -> None:
    ua = browser_user_agent("JobIndexBot/0.1 (+https://github.com/x/y)")
    assert ua == "Mozilla/5.0 (compatible; JobIndexBot/0.1; +https://github.com/x/y)"
    assert browser_user_agent("JobIndexBot/0.1") == "Mozilla/5.0 (compatible; JobIndexBot/0.1)"
    assert browser_user_agent(ua) == ua


def test_host_allow_list() -> None:
    hosts = frozenset({"jobs.acme.test", ".blob.core.windows.net"})
    assert host_allowed("jobs.acme.test", hosts)
    assert host_allowed("siteresource.blob.core.windows.net", hosts)
    assert not host_allowed("www.googletagmanager.com", hosts)
    assert not host_allowed("evil-jobs.acme.test", hosts)


def _page() -> BrowserPage:
    return BrowserPage(page=cast(Any, None), hosts=frozenset())


def test_disallowed_request_fails_the_page() -> None:
    page = _page()
    page.check()  # nothing wrong yet
    page.disallowed.append(("https://a.test/api/jobs", "Disallow: /api/"))
    with pytest.raises(BrowserBlockedError, match=r"robots\.txt disallows"):
        page.check()


def test_refusal_fails_the_page() -> None:
    page = _page()
    page.refused.append(("https://a.test/api/jobs", "403"))
    with pytest.raises(BrowserBlockedError) as info:
        page.check()
    assert info.value.status_code == 403


def test_challenge_pages_are_recognised() -> None:
    assert CHALLENGE_PATTERNS.search("<title>Just a moment...</title>")
    assert CHALLENGE_PATTERNS.search('<div class="g-recaptcha" data-sitekey="x">')
    assert not CHALLENGE_PATTERNS.search("<h1>Software Engineer</h1>")


async def test_admit_checks_robots_and_takes_a_turn() -> None:
    http, seen = mock_http(
        {"a.test/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /api/\n")}
    )
    async with http:
        assert await http.admit("https://a.test/search") == (True, None)
        assert await http.admit("https://a.test/api/jobs") == (False, "Disallow: /api/")
    assert [r.url.path for r in seen] == ["/robots.txt"]  # admit itself sends nothing


def test_bot_check_scripts_are_never_loaded() -> None:
    assert CHALLENGE_URLS.search(
        "https://jobs.x.test/cdn-cgi/challenge-platform/scripts/jsd/main.js"
    )
    assert CHALLENGE_URLS.search("https://www.google.com/recaptcha/api.js")
    assert not CHALLENGE_URLS.search("https://jobs.x.test/api/jobs/search")
