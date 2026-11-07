from jobindex.robots import RobotsTxt

UA = "JobIndexBot/0.1 (+https://example.com/bot)"


def test_longest_match_wins_and_allow_breaks_ties() -> None:
    robots = RobotsTxt.parse(
        "User-agent: *\nDisallow: /jobs\nAllow: /jobs/public\nDisallow: /a\nAllow: /a\n"
    )
    assert not robots.can_fetch(UA, "https://x.test/jobs/1")
    assert robots.can_fetch(UA, "https://x.test/jobs/public/1")
    assert robots.can_fetch(UA, "https://x.test/a")  # tie -> allow
    assert robots.can_fetch(UA, "https://x.test/other")


def test_wildcards_and_end_anchor() -> None:
    robots = RobotsTxt.parse("User-agent: *\nDisallow: /*/invite/\nDisallow: /*.pdf$\n")
    assert not robots.can_fetch(UA, "https://x.test/en/invite/abc")
    assert not robots.can_fetch(UA, "https://x.test/files/a.pdf")
    assert robots.can_fetch(UA, "https://x.test/files/a.pdf?download=1")
    assert robots.can_fetch(UA, "https://x.test/invite")


def test_specific_group_beats_star_and_groups_merge() -> None:
    robots = RobotsTxt.parse(
        "User-agent: LinkedInBot\nAllow: /v1/companies/\n"
        "User-agent: *\nDisallow: /\n"
        "User-agent: jobindexbot\nDisallow: /private\n"
        "User-agent: JobIndexBot\nDisallow: /secret\nCrawl-delay: 2\n"
    )
    assert robots.can_fetch(UA, "https://x.test/v1/companies/acme")  # own group, no "/" rule
    assert not robots.can_fetch(UA, "https://x.test/private/1")
    assert not robots.can_fetch(UA, "https://x.test/secret")
    assert robots.crawl_delay(UA) == 2.0
    assert not robots.can_fetch("OtherBot/1.0", "https://x.test/v1/companies/acme")
    assert robots.can_fetch("LinkedInBot/1.0", "https://x.test/v1/companies/acme")


def test_smartrecruiters_robots_blocks_us() -> None:
    robots = RobotsTxt.parse(
        "User-agent: LinkedInBot\nAllow: /v1/companies/\nUser-agent: *\nDisallow: /"
    )
    assert not robots.can_fetch(UA, "https://api.smartrecruiters.com/v1/companies/X/postings")


def test_empty_disallow_and_comments() -> None:
    robots = RobotsTxt.parse("# hi\nUser-agent: * # everyone\nDisallow:\n")
    assert robots.can_fetch(UA, "https://x.test/anything")
    assert RobotsTxt.disallow_all().can_fetch(UA, "https://x.test/") is False
    assert RobotsTxt.allow_all().can_fetch(UA, "https://x.test/") is True


def test_sitemaps_are_collected() -> None:
    robots = RobotsTxt.parse(
        "Sitemap: https://x.test/sitemap.xml\nUser-agent: *\nDisallow: /a\n"
        "Sitemap: https://x.test/jobs.xml\n"
    )
    assert robots.sitemaps == ["https://x.test/sitemap.xml", "https://x.test/jobs.xml"]
    assert not robots.can_fetch(UA, "https://x.test/a")


def test_explain_returns_deciding_rule() -> None:
    robots = RobotsTxt.parse("User-agent: *\nDisallow: /jobs\nAllow: /jobs/public\n")
    assert robots.explain(UA, "https://x.test/jobs/1") == (False, "Disallow: /jobs")
    assert robots.explain(UA, "https://x.test/jobs/public/1") == (True, "Allow: /jobs/public")
    assert robots.explain(UA, "https://x.test/other") == (True, None)
