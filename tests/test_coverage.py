"""Validates coverage/platforms.yaml and the COVERAGE.md rendering (no network)."""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from jobindex.connectors.registry import default_registry
from jobindex.coverage import (
    PlanError,
    Results,
    crawl_done_platforms,
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
        for url in p.examples:  # every example must be recognised offline
            assert connector.detect(url), f"{p.key} does not detect {url}"
        assert len({connector.detect(u) for u in p.examples}) >= 2, "examples must differ"


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
    assert "| WP Job Manager | Job board | yes |" in report  # an easy todo platform
