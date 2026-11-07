"""Teamtailor connector.

Fixtures (captured 2026-11-07):
    oatly_jobs.rss              GET https://oatly.teamtailor.com/jobs.rss (4 of 25 items:
                                remoteStatus fully / hybrid / onsite / none)
    lunar_jobs.rss              GET https://lunar.teamtailor.com/jobs.rss (1 of 16 items;
                                links point at the custom domain jobs.lunar.app)
    jobs_lunar_app_head.html    <head> of https://jobs.lunar.app/jobs (custom domain)
"""

import re

import httpx
import pytest

from jobindex.connectors import teamtailor
from jobindex.connectors.base import ConnectorError
from jobindex.connectors.registry import ResolvedBoard, default_registry
from jobindex.connectors.teamtailor import TeamtailorConnector
from jobindex.http import HttpClient
from jobindex.models.enums import RemoteType
from tests.fakes import load_fixture, mock_http


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://oatly.teamtailor.com", "oatly.teamtailor.com"),
        ("https://oatly.teamtailor.com/jobs", "oatly.teamtailor.com"),
        ("https://oatly.teamtailor.com/jobs/6512345-barista", "oatly.teamtailor.com"),
        (
            "https://oatly.teamtailor.com/jobs/6512345-barista/applications/new",
            "oatly.teamtailor.com",
        ),
        ("https://Lunar.teamtailor.com/jobs.rss", "lunar.teamtailor.com"),
        ("https://www.teamtailor.com/en/", None),
        ("https://support.teamtailor.com/en/articles/1", None),
        ("https://jobs.lunar.app/jobs", None),  # custom domain: recognised from the page
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert TeamtailorConnector.detect(url) == expected


async def test_custom_domain_resolves_to_its_own_host() -> None:
    page = load_fixture("teamtailor", "jobs_lunar_app_head.html")
    http, _ = mock_http({"jobs.lunar.app/jobs": httpx.Response(200, text=page)})
    async with http:
        board = await default_registry().resolve_url("https://jobs.lunar.app/jobs", http)
    assert board == ResolvedBoard("teamtailor", "jobs.lunar.app")


def test_rss_link_alone_is_not_teamtailor() -> None:
    page = (
        '<link rel="alternate" type="application/rss+xml" href="https://blog.example.com/jobs.rss">'
    )
    assert TeamtailorConnector.detect_in_html(page) == {}


async def test_fetch_jobs_from_fixture() -> None:
    http, _ = mock_http(
        {
            "oatly.teamtailor.com/jobs.rss": httpx.Response(
                200, text=load_fixture("teamtailor", "oatly_jobs.rss")
            ),
            "jobs.lunar.app/jobs.rss": httpx.Response(
                200, text=load_fixture("teamtailor", "lunar_jobs.rss")
            ),
        }
    )
    async with http:
        oatly = await TeamtailorConnector(http).fetch_jobs("oatly.teamtailor.com")
        (security,) = await TeamtailorConnector(http).fetch_jobs("jobs.lunar.app")
    assert [j.remote_type for j in oatly] == [
        RemoteType.REMOTE,  # fully
        RemoteType.HYBRID,
        RemoteType.ONSITE,
        RemoteType.UNKNOWN,  # none = not specified
    ]
    assert security.external_id == "f589b58e-bbdf-42ea-a338-21ca67f523ce"  # guid
    assert security.title == "Senior Security Engineer"
    assert (security.city, security.country) == ("Aarhus", "Denmark")
    assert security.location_raw == "Aarhus, Denmark"
    assert security.posted_at is not None
    assert security.posted_at.isoformat() == "2026-11-06T15:15:59+01:00"
    assert security.description_html is not None
    assert security.description_html.startswith("<p><span>Banking hasn't changed")
    assert security.source_url == "https://jobs.lunar.app/jobs/8681680-senior-security-engineer"


def _big_feed(n: int) -> str:
    one = load_fixture("teamtailor", "lunar_jobs.rss")
    head, item = one.split("<item>", 1)
    item = "<item>" + item.split("</item>", 1)[0] + "</item>"
    items = [
        item.replace("f589b58e-bbdf-42ea-a338-21ca67f523ce", f"guid-{i}").replace(
            "/jobs/8681680-", f"/jobs/{9000000 + i}-"
        )
        for i in range(n)
    ]
    return head + "\n".join(items) + "</channel></rss>"


async def test_large_feed_is_crosschecked_with_html(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(teamtailor, "CROSSCHECK_FROM", 3)
    feed = _big_feed(3)
    pages = {
        "1": '<a href="/jobs/9000000-a">a</a><a href="/jobs/9000001-b">b</a>',
        "2": '<a href="/jobs/9000002-c">c</a><a href="/jobs/9000003-d">d</a>',  # not in feed
        "3": "",
    }

    def html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=pages[request.url.params["page"]])

    http, _ = mock_http(
        {"jobs.lunar.app/jobs.rss": httpx.Response(200, text=feed), "jobs.lunar.app/jobs": html}
    )
    async with http:
        with pytest.raises(ConnectorError, match="feed lacks 1 of 4 jobs"):
            await TeamtailorConnector(http).fetch_jobs("jobs.lunar.app")
        pages["2"] = '<a href="/jobs/9000002-c">c</a>'
        assert len(await TeamtailorConnector(http).fetch_jobs("jobs.lunar.app")) == 3


@pytest.mark.parametrize("body", ["<html></html>", "nope", '<rss><channel x="1"'])
async def test_bad_feed_is_refused(body: str) -> None:
    http, _ = mock_http({"x.teamtailor.com/jobs.rss": httpx.Response(200, text=body)})
    async with http:
        with pytest.raises(ConnectorError):
            await TeamtailorConnector(http).fetch_jobs("x.teamtailor.com")


@pytest.mark.live
@pytest.mark.parametrize("board", ["oatly.teamtailor.com", "jobs.lunar.app"])
async def test_live(board: str) -> None:
    async with HttpClient.from_settings() as http:
        jobs = await TeamtailorConnector(http).fetch_jobs(board)
    assert len(jobs) > 0
    assert all(j.title and j.description_html and j.source_url for j in jobs)
    assert all(re.search(r"/jobs/\d+-", j.source_url or "") for j in jobs)
