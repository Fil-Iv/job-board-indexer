"""Validates coverage/platforms.yaml and the COVERAGE.md rendering (no network)."""

from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from jobindex.connectors.base import BaseConnector
from jobindex.connectors.registry import default_registry
from jobindex.coverage import (
    PlanError,
    Results,
    _render_blocked,
    crawl_done_platforms,
    load_blocked,
    load_platforms,
    render_report,
)
from tests.fakes import load_fixture, mock_http

# The 78 platforms in scope (58 ATS + 20 job-board platforms).
EXPECTED_ATS = {
    "ADP",
    "ApplicantPro",
    "Ashby",
    "Avature",
    "BambooHR",
    "BrassRing",
    "Breezy HR",
    "CareerPlug",
    "Comeet",
    "CSOD",
    "Dayforce",
    "Deel",
    "Dover",
    "Eightfold AI",
    "FirstStage",
    "Freshteam",
    "Gem.com",
    "GoHire",
    "Greenhouse",
    "HiBob",
    "HireBridge",
    "HireHive",
    "Hireology",
    "HiringThing",
    "iCIMS",
    "iSolvedHire",
    "JazzHR",
    "Jobvite",
    "JOIN.com",
    "Kula.ai",
    "Lever.co",
    "Manatal",
    "Oracle Recruiting",
    "PageUp",
    "Paradox",
    "Paycom",
    "Paycor",
    "Paylocity",
    "Personio",
    "Phenom People",
    "Pinpoint",
    "Polymer",
    "Recooty",
    "Recruitee",
    "Rippling",
    "Rival",
    "SAP SuccessFactors",
    "SmartRecruiters",
    "talentReef",
    "Taleo",
    "Teamtailor",
    "Trakstar",
    "TriNet",
    "UltiPro",
    "WeRecruit",
    "Workable",
    "Workday",
    "Zoho Recruit",
}
EXPECTED_JOBBOARDS = {
    "Niceboard",
    "WP Job Manager",
    "JobKit",
    "Jobboardly",
    "Jobiqo",
    "JBoard.io",
    "JobBoardFire",
    "SmartJobBoard",
    "JobBoard.io",
    "Wiley Madgex",
    "Talenetic",
    "JobMount",
    "CareerSiteCloud",
    "JobBoard.com",
    "Artha",
    "jobboardsoftware.com",
    "jobBoardHQ",
    "Nodex",
    "eJobBoard",
    "ejobsitesoftware.com",
}


def test_plan_lists_exactly_the_78_platforms() -> None:
    platforms = load_platforms()
    assert {p.name for p in platforms if p.type == "ats"} == EXPECTED_ATS
    assert {p.name for p in platforms if p.type == "jobboard"} == EXPECTED_JOBBOARDS
    assert len(platforms) == 78


def test_done_platforms_match_registered_connectors() -> None:
    platforms = load_platforms()
    done = {p.key for p in platforms if p.status == "done"}
    assert done == set(default_registry().platforms)
    for p in platforms:
        if p.status != "done":
            continue
        assert p.connector == f"jobindex.connectors.{p.key}"
        connector = default_registry().get(p.key)
        # page_fingerprint (generic connectors) or an own detect_in_html (WP Job Manager)
        custom_domains = getattr(connector, "page_fingerprint", None) is not None or any(
            "detect_in_html" in vars(cls) for cls in connector.__mro__ if cls is not BaseConnector
        )
        for url in p.examples:
            # Every example must be recognised offline, except custom-domain boards of
            # platforms that recognise them from the page (make coverage resolves those).
            assert connector.detect(url) or custom_domains, f"{p.key} does not detect {url}"
        boards = {connector.detect(u) or urlsplit(u).netloc for u in p.examples}
        assert len(boards) >= (1 if p.partial else 2), "examples must differ"


def test_verified_entries_have_two_examples() -> None:
    for p in load_platforms():
        if p.verified is not None and p.status != "done":
            assert len(p.examples) >= 2, f"{p.name} is marked verified but lacks 2 examples"


def test_invalid_plan_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "platforms.yaml"
    bad.write_text(
        "- {name: X, key: x, type: ats, status: done, public_api: 'yes', api: a,"
        " robots: allowed, examples: [https://a]}\n"
    )
    with pytest.raises(PlanError, match="connector"):
        load_platforms(bad)
    bad.write_text(
        "- {name: X, key: x, type: ats, status: todo, public_api: 'yes', api: a, robots: allowed}\n"
    )
    with pytest.raises(PlanError, match="tier"):
        load_platforms(bad)


def test_partial_platform_may_have_one_example(tmp_path: Path) -> None:
    plan = tmp_path / "platforms.yaml"
    entry = (
        "- {{name: X, key: x, type: ats, status: {status}, public_api: 'no', api: a,"
        " robots: allowed, connector: jobindex.connectors.x, examples: [https://a]{extra}}}\n"
    )
    plan.write_text(entry.format(status="done", extra=""))
    with pytest.raises(PlanError, match="2 example URLs"):
        load_platforms(plan)
    plan.write_text(entry.format(status="done", extra=", partial: only one live board found"))
    assert load_platforms(plan)[0].partial == "only one live board found"
    plan.write_text(entry.format(status="todo", extra=", tier: easy, partial: x"))
    with pytest.raises(PlanError, match="partial is only for done"):
        load_platforms(plan)


def test_blocked_list_loads_and_every_item_has_evidence() -> None:
    blocked = {b.platform: b for b in load_blocked()}
    assert all(b.evidence for b in blocked.values())
    tr = blocked["talentReef"]
    assert tr.scope == "platform"
    assert [ev.policy for ev in tr.evidence if ev.policy] == [
        "751-byte JS shell, no job data",
        "internal host (owner decision)",
    ]


_EV = (
    '{url: "https://x.test/jobs", checked_at: "2026-11-08T00:00:00+00:00", http_status: 200, '
    'robots_url: "https://x.test/robots.txt", robots_http_status: 200, robots_rule: null'
)


def test_blocked_evidence_needs_a_refusal_or_a_policy_reason(tmp_path: Path) -> None:
    path = tmp_path / "blocked.yaml"
    item = "blocked_by_site:\n  - {platform: X, scope: platform, impact: i, evidence: [%s}]}\n"
    path.write_text(item % _EV, encoding="utf-8")
    with pytest.raises(PlanError, match="policy reason"):
        load_blocked(path)
    path.write_text(item % (_EV + ', policy: "internal host"'), encoding="utf-8")
    (b,) = load_blocked(path)
    assert "| 200 — not used: internal host | — |" in "\n".join(_render_blocked([b]))


def test_failure_keeps_last_success() -> None:
    results = Results()
    t1 = datetime(2026, 11, 1, tzinfo=UTC)
    t2 = datetime(2026, 11, 2, tzinfo=UTC)
    results.record("https://x", t1, jobs=12)
    r = results.record("https://x", t2, error="FetchError: HTTP 503")
    assert (r.last_status, r.last_jobs_found) == ("failed", None)
    assert (r.last_success_at, r.last_success_jobs) == (t1.isoformat(), 12)


def test_results_roundtrip(tmp_path: Path) -> None:
    results = Results()
    results.record("https://x", datetime(2026, 11, 1, tzinfo=UTC), jobs=3)
    results.save(tmp_path / "r.json")
    assert Results.load(tmp_path / "r.json") == results


async def test_crawl_and_render(monkeypatch: pytest.MonkeyPatch) -> None:
    platforms = [p for p in load_platforms() if p.key in {"greenhouse", "smartrecruiters"}]
    # Ubisoft2 opted out via www.smartrecruiters.com/robots.txt -> the connector refuses it.
    robots_sr = "User-agent: *\nDisallow: /Ubisoft2/*\n"
    http, _ = mock_http(
        {
            "boards-api.greenhouse.io/v1/boards/discord/jobs": httpx.Response(
                200, text=load_fixture("greenhouse", "discord_jobs.json")
            ),
            # gitlab is not routed -> 404
            "www.smartrecruiters.com/robots.txt": httpx.Response(200, text=robots_sr),
        }
    )
    results = Results()
    async with http:
        await crawl_done_platforms(platforms, results, http)

    discord = results.examples["https://job-boards.greenhouse.io/discord"]
    assert (discord.last_status, discord.last_jobs_found) == ("ok", 3)
    gitlab = results.examples["https://job-boards.greenhouse.io/gitlab"]
    assert gitlab.last_status == "failed"
    assert gitlab.last_error is not None
    assert "404" in gitlab.last_error
    sr = results.examples["https://jobs.smartrecruiters.com/Ubisoft2"]
    assert sr.last_status == "blocked"

    plan = load_platforms()
    done = sum(p.status == "done" for p in plan)
    report = render_report(plan, results, datetime(2026, 11, 7, tzinfo=UTC))
    assert f"**78 platforms** (58 ATS, 20 job boards): **{done} done**" in report
    assert "| https://job-boards.greenhouse.io/discord | 3 |" in report
    assert "blocked (robots.txt) | never |" in report
    assert "### Easy — public JSON API" in report
    assert "| CareerPlug | ATS | unknown |" in report  # a todo platform row
