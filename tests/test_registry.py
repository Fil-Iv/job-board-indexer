from pathlib import Path

import httpx
import pytest

from jobindex.connectors.base import urls_in_html
from jobindex.connectors.registry import (
    Registry,
    ResolvedBoard,
    UnknownPlatformError,
    default_registry,
)
from jobindex.http import HttpClient
from tests.fakes import FakeConnector, mock_http


def test_default_registry_has_all_connectors() -> None:
    # One module per platform, named after its key (CLAUDE.md): every module registers.
    modules = sorted(
        p.stem
        for p in (Path(__file__).parent.parent / "jobindex" / "connectors").glob("*.py")
        if p.stem not in {"__init__", "base", "registry", "generic_jsonld"}
    )
    assert default_registry().platforms == modules
    assert {"ashby", "greenhouse", "lever", "workday"} <= set(modules)


def test_registry_basics() -> None:
    registry = Registry()
    registry.add(FakeConnector)
    assert registry.detect("https://jobs.fake.test/acme") == ResolvedBoard("fake", "acme")
    assert registry.detect("https://other.example/jobs") is None
    assert registry.get("fake") is FakeConnector
    with pytest.raises(UnknownPlatformError):
        registry.get("nope")

    class Other(FakeConnector):
        pass

    with pytest.raises(ValueError, match="already registered"):
        registry.add(Other)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://boards.greenhouse.io/embed/job_board?for=figma", ("greenhouse", "figma")),
        ("https://jobs.lever.co/spotify/abc/apply", ("lever", "spotify")),
        ("https://jobs.ashbyhq.com/linear/embed", ("ashby", "linear")),
        ("https://careers.smartrecruiters.com/BoschGroup", ("smartrecruiters", "BoschGroup")),
    ],
)
def test_detect_hosted_urls(url: str, expected: tuple[str, str]) -> None:
    assert default_registry().detect(url) == expected


def test_urls_in_html_handles_escaping() -> None:
    html = (
        '<iframe src="//boards.greenhouse.io/embed/job_board?for=a&amp;b=1"></iframe>'
        '{"u":"https:\\/\\/jobs.lever.co\\/b\\/1"} url(https://cdn.x.test/i.png)'
    )
    assert urls_in_html(html) == [
        "https://boards.greenhouse.io/embed/job_board?for=a&b=1",
        "https://jobs.lever.co/b/1",
        "https://cdn.x.test/i.png",
    ]


def test_detect_in_html_picks_most_referenced_board() -> None:
    html = (
        '<a href="https://jobs.lever.co/partner">Partner jobs</a>'
        + '<a href="https://jobs.ashbyhq.com/acme/1">x</a>' * 3
    )
    assert default_registry().detect_in_html(html) == ResolvedBoard("ashby", "acme")
    assert default_registry().detect_in_html("<html>no jobs here</html>") is None


async def test_resolve_custom_domain_page() -> None:
    page = """<html><body><h1>Careers at Acme</h1><div id="grnhse_app"></div>
      <script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script></body></html>"""
    http, seen = mock_http({"acme.test/careers": httpx.Response(200, text=page)})
    async with http:
        board = await default_registry().resolve_url("https://acme.test/careers", http)
    assert board == ResolvedBoard("greenhouse", "acme")
    assert [r.url.path for r in seen] == ["/robots.txt", "/careers"]


async def test_resolve_follows_redirect_to_hosted_board() -> None:
    http, _ = mock_http(
        {
            "acme.test/jobs": httpx.Response(
                302, headers={"Location": "https://jobs.lever.co/acme"}
            ),
            "jobs.lever.co/acme": httpx.Response(200, text="<html>Lever board</html>"),
        }
    )
    async with http:
        board = await default_registry().resolve_url("https://acme.test/jobs", http)
    assert board == ResolvedBoard("lever", "acme")


async def test_hosted_url_needs_no_request() -> None:
    http, seen = mock_http({})
    async with http:
        board = await default_registry().resolve_url("https://jobs.lever.co/acme", http)
    assert board == ResolvedBoard("lever", "acme")
    assert seen == []


@pytest.mark.live
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.figma.com/careers/", ("greenhouse", "figma")),
        ("https://linear.app/careers", ("ashby", "linear")),
        ("https://www.notion.com/careers", ("ashby", "notion")),
    ],
)
async def test_live_resolve_custom_domains(url: str, expected: tuple[str, str]) -> None:
    async with HttpClient.from_settings() as http:
        assert await default_registry().resolve_url(url, http) == expected
