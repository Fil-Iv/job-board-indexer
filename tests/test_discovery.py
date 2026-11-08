import httpx

from jobindex.access import check_access
from jobindex.discovery import (
    discover,
    evaluate_board,
    looks_like_detail_url,
    looks_like_job_url,
    parse_feed,
    parse_sitemap,
    pick_sample,
    tenant_prefix,
)
from tests.fakes import mock_http

JOB_PAGE = """<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting","title":"Engineer %s",
 "description":"<p>Do things</p>","datePosted":"2026-11-01",
 "hiringOrganization":{"@type":"Organization","name":"Acme"}}
</script></head><body></body></html>"""

INDEX = """<?xml version="1.0"?><sitemapindex>
<sitemap><loc>https://board.test/sitemap-pages.xml</loc></sitemap>
<sitemap><loc>https://board.test/sitemap-jobs.xml.gz</loc></sitemap>
</sitemapindex>"""
JOBS = """<?xml version="1.0"?><urlset>
<url><loc>https://board.test/jobs/101-engineer</loc><lastmod>2026-11-01</lastmod></url>
<url><loc><![CDATA[https://board.test/jobs/102-designer]]></loc></url>
<url><loc>https://board.test/jobs/</loc></url>
<url><loc>https://elsewhere.test/jobs/1</loc></url>
</urlset>"""
PAGES = "<urlset><url><loc>https://board.test/about</loc></url></urlset>"


def _board_routes() -> dict[str, httpx.Response]:
    import gzip

    return {
        "board.test/robots.txt": httpx.Response(
            200, text="User-agent: *\nDisallow: /admin\nSitemap: https://board.test/sitemap.xml\n"
        ),
        "board.test/jobs/": httpx.Response(200, text="<html>listing</html>"),
        "board.test/sitemap.xml": httpx.Response(200, text=INDEX),
        "board.test/sitemap-pages.xml": httpx.Response(200, text=PAGES),
        "board.test/sitemap-jobs.xml.gz": httpx.Response(200, content=gzip.compress(JOBS.encode())),
        "board.test/jobs/101-engineer": httpx.Response(200, text=JOB_PAGE % "101"),
        "board.test/jobs/102-designer": httpx.Response(200, text=JOB_PAGE % "102"),
    }


def test_looks_like_job_url() -> None:
    board = "https://board.test/jobs/"
    assert looks_like_job_url("https://board.test/jobs/101-engineer", board)
    assert looks_like_job_url("https://board.test/stellenangebote/x", board)
    assert not looks_like_job_url("https://board.test/jobs/", board)  # the listing
    assert not looks_like_job_url("https://board.test/about", board)
    assert not looks_like_job_url("https://other.test/jobs/1", board)


def test_pick_sample_prefers_detail_pages_and_spreads() -> None:
    # Real shape from a Jobboardly board: listing + category pages mixed into the sitemap.
    urls = [
        "https://b.test/jobs",
        "https://b.test/jobs/contract",
        *[f"https://b.test/jobs/race-engineer-{i:08x}" for i in range(10)],
    ]
    picks = pick_sample(urls, 3)
    assert picks == [urls[2], urls[6], urls[11]]  # first, middle, last detail page
    assert pick_sample(urls[:2], 3) == urls[:2]  # no detail pages: fall back to all
    assert looks_like_detail_url("https://b.test/jobs/1892162.html")
    assert looks_like_detail_url("https://wayve.firststage.co/jobs/k4IIfGLJ7i/view")
    assert not looks_like_detail_url("https://b.test/jobs/in-chalk-river-ontario-canada")


async def test_discover_follows_robots_sitemap_index_and_gzip() -> None:
    http, _ = mock_http(_board_routes())
    async with http:
        d = await discover(http, "https://board.test/jobs/")
    assert d.job_urls == [
        "https://board.test/jobs/101-engineer",
        "https://board.test/jobs/102-designer",
    ]
    assert "https://board.test/sitemap-jobs.xml.gz" in d.sitemaps


async def test_discover_resolves_relative_urls() -> None:
    # Seen on a live Niceboard board: a relative robots.txt Sitemap line used to be
    # fetched as ":///sitemap.xml".
    http, _ = mock_http(
        {
            "board.test/robots.txt": httpx.Response(200, text="Sitemap: /sitemap.xml\n"),
            "board.test/jobs/": httpx.Response(200, text="<html>listing</html>"),
            "board.test/sitemap.xml": httpx.Response(
                200,
                text="<urlset><url><loc>/jobs/101-engineer</loc></url>"
                "<url><loc>mailto:x@board.test</loc></url></urlset>",
            ),
        }
    )
    async with http:
        d = await discover(http, "https://board.test/jobs/")
    assert d.job_urls == ["https://board.test/jobs/101-engineer"]
    assert not [e for e in d.errors if ":///" in e]


async def test_evaluate_board_ok() -> None:
    http, _ = mock_http(_board_routes())
    async with http:
        ev = await evaluate_board(http, "https://board.test/jobs/")
    assert ev.generic_ok
    assert (ev.job_urls, ev.sampled, ev.with_jobposting) == (2, 2, 2)
    assert ev.field_coverage["title"] == 2


async def test_rss_fallback() -> None:
    rss = (
        '<?xml version="1.0"?><rss><channel><item><title>A</title>'
        "<link>https://feed.test/job/7</link></item></channel></rss>"
    )
    http, _ = mock_http(
        {
            "feed.test/": httpx.Response(
                200,
                text='<link rel="alternate" type="application/rss+xml" href="/jobs.rss">',
            ),
            "feed.test/jobs.rss": httpx.Response(200, text=rss),
        }
    )
    async with http:
        d = await discover(http, "https://feed.test/")
    assert d.feeds == ["https://feed.test/jobs.rss"]
    assert d.job_urls == ["https://feed.test/job/7"]


async def test_check_access_never_fetches_disallowed() -> None:
    http, seen = mock_http(
        {
            "api.test/robots.txt": httpx.Response(
                200, text="User-agent: LinkedInBot\nAllow: /v1/\nUser-agent: *\nDisallow: /\n"
            ),
            "api.test/v1/x": httpx.Response(200, json={"ok": True}),
            "cdn.test/x": httpx.Response(403),
        }
    )
    async with http:
        blocked = await check_access("https://api.test/v1/x", http)
        forbidden = await check_access("https://cdn.test/x", http)
    assert (blocked.robots_allowed, blocked.robots_rule, blocked.http_status) == (
        False,
        "Disallow: /",
        None,
    )
    assert blocked.robots_http_status == 200
    assert [r.url.path for r in seen if r.url.host == "api.test"] == ["/robots.txt"]
    assert forbidden.robots_allowed
    assert forbidden.http_status == 403
    assert forbidden.blocked


async def test_check_access_unreachable_robots_is_not_a_site_block() -> None:
    # Pass-2 research logged DNS failures as "Disallow: /"; an outage is not a site rule.
    http, seen = mock_http({"down.test/robots.txt": httpx.Response(503)})
    async with http:
        check = await check_access("https://down.test/jobs", http)
    assert not check.robots_allowed  # RFC 9309: still treated as disallow-all...
    assert check.robots_unreachable
    assert check.robots_rule is None
    assert not check.blocked  # ...but never reported as blocked_by_site
    assert [r.url.path for r in seen] == ["/robots.txt"]


def test_tenant_prefix_and_browse_pages() -> None:
    assert tenant_prefix("https://careers.hireology.com/sandiegopadres") == "/sandiegopadres"
    assert tenant_prefix("https://join.com/companies/comlab") == "/companies/comlab"
    assert tenant_prefix("https://x.icims.com/jobs/search?ss=1") == ""
    assert tenant_prefix("https://bloomberg.avature.net/careers/SearchJobs") == ""
    assert tenant_prefix("https://careers.butterball.com/us/en") == ""
    # Real browse pages seen in ApplicantPro / SmartJobBoard sitemaps:
    assert not looks_like_detail_url(
        "https://mwra.applicantpro.com/jobsandemployment/location/state/Massachusetts/22/"
    )
    assert not looks_like_detail_url("https://jobs.alanet.org/company/1000/preti-flaherty/")
    # ...but a tenant path that happens to say "companies" is not a browse page.
    assert looks_like_detail_url(
        "https://join.com/companies/comlab/12345678-engineer", "/companies/comlab"
    )


async def test_discover_keeps_own_tenant_on_shared_host() -> None:
    sitemap = (
        "<urlset><url><loc>https://shared.test/acme/1234567/description</loc></url>"
        "<url><loc>https://shared.test/other/7654321/description</loc></url></urlset>"
    )
    http, _ = mock_http(
        {
            "shared.test/acme": httpx.Response(200, text="<html></html>"),
            "shared.test/sitemap.xml": httpx.Response(200, text=sitemap),
        }
    )
    async with http:
        d = await discover(http, "https://shared.test/acme")
    assert d.job_urls == ["https://shared.test/acme/1234567/description"]


def test_sitemap_and_feed_urls_are_xml_unescaped() -> None:
    # JobBoard.io child sitemaps (jobs.hireheroesusa.org, 2026-11-08): with "&amp;" kept,
    # the chunk URL loses start_id and the server returns an empty urlset.
    index = (
        "<sitemapindex><sitemap><loc>https://b.test/sitemaps/jobs/job_sitemap_details.xml"
        "?finish_id=2&amp;start_id=1</loc></sitemap></sitemapindex>"
    )
    children, _ = parse_sitemap(index, "https://b.test/sitemap.xml")
    assert children == [
        "https://b.test/sitemaps/jobs/job_sitemap_details.xml?finish_id=2&start_id=1"
    ]
    urlset = (
        "<urlset><url><loc>https://b.test/j?a=1&amp;b=2</loc></url>"
        "<url><loc><![CDATA[https://b.test/k?a=1&amp;b=2]]></loc></url></urlset>"
    )
    _, entries = parse_sitemap(urlset, "https://b.test/sitemap.xml")
    assert [e.url for e in entries] == ["https://b.test/j?a=1&b=2", "https://b.test/k?a=1&amp;b=2"]
    feed = "<rss><channel><item><link>https://b.test/j?x=1&amp;y=2</link></item></channel></rss>"
    assert parse_feed(feed, "https://b.test/rss") == ["https://b.test/j?x=1&y=2"]
