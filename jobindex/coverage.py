"""Platform coverage report.

Reads ``coverage/platforms.yaml`` (the plan: every platform, its status, tier, endpoints
and example boards), live-crawls the examples of every ``done`` platform, keeps results
in ``coverage/results.json`` (so "last successful run" survives a failed run) and renders
``docs/COVERAGE.md``.

Entry point: ``scripts/coverage_report.py`` / ``make coverage``.
"""

import asyncio
import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, cast

import yaml

from jobindex.connectors.registry import Registry, default_registry
from jobindex.http import FetchError, HttpClient, RobotsDisallowedError

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
PLATFORMS_FILE = ROOT / "coverage" / "platforms.yaml"
BLOCKED_FILE = ROOT / "coverage" / "blocked_by_site.yaml"
RESULTS_FILE = ROOT / "coverage" / "results.json"
REPORT_FILE = ROOT / "docs" / "COVERAGE.md"

PlatformType = Literal["ats", "jobboard"]
Status = Literal["todo", "in_progress", "done"]
Tier = Literal["easy", "medium", "hard"]

TYPES = ("ats", "jobboard")
STATUSES = ("todo", "in_progress", "done")
TIERS = ("easy", "medium", "hard")
PUBLIC_API = ("yes", "partial", "feed", "no", "unknown")
ROBOTS = ("allowed", "blocked", "per-tenant", "unknown")
#: Would the generic sitemap/RSS + schema.org JobPosting connector cover the platform?
#: yes = every evaluated board passed; partial = some boards / some pages; no = evaluated,
#: failed; untested = not evaluated (``scripts/evaluate_generic.py``).
GENERIC = ("yes", "partial", "no", "untested")
BLOCK_SCOPES = ("platform", "api", "tenant")
TIER_LABELS = {
    "easy": "Easy — public JSON API (or structured XML/RSS feed)",
    "medium": "Medium — structured HTML / embedded JSON",
    "hard": "Hard — JS-heavy, session tokens, bot protection (Playwright?)",
}


class PlanError(ValueError):
    """platforms.yaml is invalid."""


@dataclass(frozen=True, slots=True)
class Platform:
    name: str
    key: str
    type: PlatformType
    status: Status
    tier: Tier | None
    public_api: str
    api: str
    url_patterns: tuple[str, ...]
    examples: tuple[str, ...]
    robots: str
    verified: date | None
    notes: str
    connector: str | None = None
    generic: str = "untested"
    #: ``{example URL: employer}`` for platforms whose data never names the employer
    #: (HiBob, ADP, PageUp); ``seed-demo`` stores it as ``sources.company_name``.
    example_companies: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class BlockEvidence:
    """One request a site refused (robots.txt rule or HTTP 401/403/429) — see
    ``coverage/blocked_by_site.yaml``."""

    url: str
    checked_at: str
    http_status: int | None  # None = not fetched because robots.txt disallows it
    robots_url: str
    robots_http_status: int | None
    robots_rule: str | None


@dataclass(frozen=True, slots=True)
class BlockedPlatform:
    platform: str
    scope: str  # platform = no allowed route; api = API blocked, HTML route open; tenant
    impact: str
    evidence: tuple[BlockEvidence, ...]


@dataclass(slots=True)
class ExampleResult:
    """Outcome of crawling one example board; persisted in results.json."""

    url: str
    last_run_at: str | None = None
    last_status: Literal["ok", "failed", "blocked"] | None = None
    last_jobs_found: int | None = None
    last_error: str | None = None
    last_success_at: str | None = None
    last_success_jobs: int | None = None


@dataclass(slots=True)
class Results:
    examples: dict[str, ExampleResult] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = RESULTS_FILE) -> "Results":
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls({url: ExampleResult(**data) for url, data in raw.get("examples", {}).items()})

    def save(self, path: Path = RESULTS_FILE) -> None:
        data = {"examples": {url: asdict(r) for url, r in sorted(self.examples.items())}}
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def record(
        self,
        url: str,
        at: datetime,
        *,
        jobs: int | None = None,
        error: str | None = None,
        blocked: bool = False,
    ) -> ExampleResult:
        """Store a run outcome. A failure never erases the previous success."""
        result = self.examples.setdefault(url, ExampleResult(url=url))
        result.last_run_at = at.isoformat(timespec="seconds")
        if error is None:
            result.last_status = "ok"
            result.last_jobs_found = jobs
            result.last_error = None
            result.last_success_at = result.last_run_at
            result.last_success_jobs = jobs
        else:
            result.last_status = "blocked" if blocked else "failed"
            result.last_jobs_found = None
            result.last_error = error
        return result


# ---------------------------------------------------------------------------
# Loading and validating the plan
# ---------------------------------------------------------------------------


def _choice(entry: dict[str, Any], key: str, allowed: tuple[str, ...], where: str) -> str:
    value = entry.get(key)
    if value is True:  # YAML 1.1 turns a bare `yes` into True
        value = "yes"
    elif value is False:
        value = "no"
    if value not in allowed:
        raise PlanError(f"{where}: {key}={value!r} must be one of {', '.join(allowed)}")
    return str(value)


def _platform(entry: dict[str, Any], index: int) -> Platform:
    where = f"entry {index} ({entry.get('name', '?')})"
    for required in ("name", "key", "api"):
        if not entry.get(required):
            raise PlanError(f"{where}: missing {required!r}")
    status = _choice(entry, "status", STATUSES, where)
    tier = entry.get("tier")
    if status != "done":
        tier = _choice(entry, "tier", TIERS, where)
    elif tier is not None:
        raise PlanError(f"{where}: done platforms have no tier")
    verified = entry.get("verified")
    if verified is not None and not isinstance(verified, date):
        raise PlanError(f"{where}: verified must be a date (YYYY-MM-DD) or null")
    examples = tuple(str(u) for u in entry.get("examples") or [])
    connector = entry.get("connector")
    if status == "done":
        if not connector:
            raise PlanError(f"{where}: done platforms need a connector module")
        if len(examples) < 2:
            raise PlanError(f"{where}: done platforms need at least 2 example URLs")
    companies = entry.get("example_companies") or {}
    if not isinstance(companies, dict) or not set(companies) <= set(examples):
        raise PlanError(f"{where}: example_companies must map example URLs to names")
    return Platform(
        name=str(entry["name"]),
        key=str(entry["key"]),
        type=cast(PlatformType, _choice(entry, "type", TYPES, where)),
        status=cast(Status, status),
        tier=cast(Tier | None, tier),
        public_api=_choice(entry, "public_api", PUBLIC_API, where),
        api=str(entry["api"]),
        url_patterns=tuple(str(p) for p in entry.get("url_patterns") or []),
        examples=examples,
        robots=_choice(entry, "robots", ROBOTS, where),
        verified=verified,
        notes=str(entry.get("notes") or ""),
        connector=str(connector) if connector else None,
        generic=_choice({"generic": entry.get("generic", "untested")}, "generic", GENERIC, where),
        example_companies=tuple((str(u), str(n)) for u, n in companies.items()),
    )


def load_platforms(path: Path = PLATFORMS_FILE) -> list[Platform]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise PlanError(f"{path}: expected a list of platforms")
    platforms = [_platform(entry, i) for i, entry in enumerate(raw)]
    for attr in ("name", "key"):
        dupes = [k for k, n in Counter(getattr(p, attr) for p in platforms).items() if n > 1]
        if dupes:
            raise PlanError(f"duplicate {attr}: {', '.join(dupes)}")
    return platforms


def load_blocked(path: Path = BLOCKED_FILE) -> list[BlockedPlatform]:
    """The blocked_by_site evidence list. Every item needs at least one refused request:
    a robots.txt rule (robots_rule) or an HTTP 401/403/429. Outages (DNS errors,
    robots.txt 5xx) are not blocks and are rejected."""
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    items = raw.get("blocked_by_site") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        raise PlanError(f"{path}: expected a top-level 'blocked_by_site' list")
    blocked = []
    for i, item in enumerate(items):
        where = f"blocked_by_site[{i}] ({item.get('platform', '?')})"
        scope = _choice(item, "scope", BLOCK_SCOPES, where)
        evidence = []
        for ev in item.get("evidence") or []:
            status, rule = ev.get("http_status"), ev.get("robots_rule")
            if not rule and status not in (401, 403, 429):
                raise PlanError(f"{where}: {ev.get('url')} has no robots rule or 401/403/429")
            evidence.append(
                BlockEvidence(
                    url=str(ev["url"]),
                    checked_at=str(ev["checked_at"]),
                    http_status=status,
                    robots_url=str(ev["robots_url"]),
                    robots_http_status=ev.get("robots_http_status"),
                    robots_rule=str(rule) if rule else None,
                )
            )
        if not evidence:
            raise PlanError(f"{where}: needs at least one evidence entry")
        blocked.append(
            BlockedPlatform(str(item["platform"]), scope, str(item["impact"]), tuple(evidence))
        )
    return blocked


# ---------------------------------------------------------------------------
# Live crawl of done platforms
# ---------------------------------------------------------------------------


async def crawl_example(
    platform: Platform, url: str, http: HttpClient, registry: Registry
) -> tuple[int | None, str | None, bool]:
    """Return (jobs_found, error, blocked_by_robots) for one example board.

    Only the job list is fetched (no per-job detail requests): "jobs found" is the size of
    the board, which is what coverage needs.
    """
    connector_cls = registry.get(platform.key)
    try:
        identifier = connector_cls.detect(url)
        if identifier is None:
            board = await registry.resolve_url(url, http)
            if board is None or board.platform != platform.key:
                return None, f"URL not recognised by the {platform.key} connector", False
            identifier = board.board_identifier
        jobs = await connector_cls(http).fetch_jobs(identifier)
    except RobotsDisallowedError as exc:
        return None, f"blocked by robots.txt ({exc.url})", True
    except (FetchError, Exception) as exc:  # report every failure, keep going
        return None, f"{type(exc).__name__}: {exc}"[:300], False
    return len(jobs), None, False


async def crawl_done_platforms(
    platforms: list[Platform],
    results: Results,
    http: HttpClient,
    registry: Registry | None = None,
    *,
    only: set[str] | None = None,
) -> None:
    registry = registry or default_registry()
    targets = [p for p in platforms if p.status == "done" and (only is None or p.key in only)]

    async def run_platform(platform: Platform) -> None:
        # Examples of one platform share hosts, so run them one after another; the
        # HttpClient's per-host limiter would serialise them anyway.
        for url in platform.examples:
            jobs, error, blocked = await crawl_example(platform, url, http, registry)
            results.record(url, datetime.now(UTC), jobs=jobs, error=error, blocked=blocked)
            log.info("%s %s -> %s", platform.key, url, error or f"{jobs} jobs")

    await asyncio.gather(*(run_platform(p) for p in targets))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _jobs_cell(result: ExampleResult | None) -> str:
    if result is None or result.last_status is None:
        return "not run"
    if result.last_status == "ok":
        return str(result.last_jobs_found)
    label = "blocked (robots.txt)" if result.last_status == "blocked" else "failed"
    return f"{label}: {_cell(result.last_error or '')}" if label == "failed" else label


def _success_cell(result: ExampleResult | None) -> str:
    if result is None or result.last_success_at is None:
        return "never"
    return result.last_success_at.replace("T", " ").replace("+00:00", " UTC")


def _render_blocked(blocked: list[BlockedPlatform]) -> list[str]:
    lines = [
        "## Blocked by site",
        "",
        f"{len(blocked)} platforms refuse at least part of what we need (source: "
        "`coverage/blocked_by_site.yaml`; scope: platform = no allowed route, api = the "
        "JSON API is refused but HTML pages are open, tenant = only some employers/boards).",
        "",
        "| Platform | Scope | Refused request | Status | robots.txt rule | Checked (UTC) |",
        "|---|---|---|---|---|---|",
    ]
    for b in blocked:
        for i, ev in enumerate(b.evidence):
            head = (b.platform, b.scope) if i == 0 else ("", "")
            status = str(ev.http_status) if ev.http_status is not None else "not fetched"
            rule = f"`{_cell(ev.robots_rule)}` ({ev.robots_url})" if ev.robots_rule else "—"
            lines.append(
                f"| {' | '.join(head)} | `{_cell(ev.url)}` | {status} | {rule} | "
                f"{ev.checked_at.replace('T', ' ').replace('+00:00', '')} |"
            )
    return [*lines, ""]


def render_report(
    platforms: list[Platform],
    results: Results,
    generated_at: datetime,
    blocked: list[BlockedPlatform] | None = None,
) -> str:
    by_status = Counter(p.status for p in platforms)
    by_type = Counter(p.type for p in platforms)
    todo = [p for p in platforms if p.status != "done"]
    by_tier = Counter(p.tier for p in todo)
    lines = [
        "# Platform coverage",
        "",
        "<!-- Generated by scripts/coverage_report.py from coverage/platforms.yaml."
        " Do not edit by hand: edit the YAML and run `make coverage`. -->",
        "",
        f"Generated {generated_at.strftime('%Y-%m-%d %H:%M UTC')}. "
        f"**{len(platforms)} platforms** ({by_type['ats']} ATS, {by_type['jobboard']} job boards): "
        f"**{by_status['done']} done**, {by_status['in_progress']} in progress, "
        f"{by_status['todo']} todo "
        f"(easy {by_tier['easy']} / medium {by_tier['medium']} / hard {by_tier['hard']}).",
        "",
        "Jobs found = size of the board's job list in the latest live crawl. "
        "Last success = the latest run in which that example crawled successfully.",
        "",
        "## All platforms",
        "",
        "| Platform | Type | Status | Example URLs | Jobs found | Last successful run |",
        "|---|---|---|---|---|---|",
    ]
    status_order = {"done": 0, "in_progress": 1, "todo": 2}
    tier_order = {None: 0, "easy": 1, "medium": 2, "hard": 3}
    ordered = sorted(
        platforms, key=lambda p: (status_order[p.status], tier_order[p.tier], p.name.lower())
    )
    for p in ordered:
        status = p.status if p.status == "done" else f"{p.status} ({p.tier})"
        type_label = "ATS" if p.type == "ats" else "Job board"
        if not p.examples:
            lines.append(f"| {p.name} | {type_label} | {status} | — | — | — |")
            continue
        for i, url in enumerate(p.examples):
            result = results.examples.get(url)
            crawled = p.status == "done"
            head = (p.name, type_label, status) if i == 0 else ("", "", "")
            jobs = _jobs_cell(result) if crawled else "—"
            success = _success_cell(result) if crawled else "—"
            lines.append(f"| {' | '.join(head)} | {url} | {jobs} | {success} |")

    lines += ["", "## Work plan", ""]
    for tier in TIERS:
        tier_platforms = sorted((p for p in todo if p.tier == tier), key=lambda p: p.name.lower())
        lines += [
            f"### {TIER_LABELS[tier]} ({len(tier_platforms)})",
            "",
            "| Platform | Type | Public API | Endpoint | URL patterns | robots.txt | Generic "
            "| Verified | Notes |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for p in tier_platforms:
            patterns = "<br>".join(f"`{_cell(u)}`" for u in p.url_patterns) or "—"
            verified = p.verified.isoformat() if p.verified else "no"
            lines.append(
                f"| {p.name} | {'ATS' if p.type == 'ats' else 'Job board'} | {p.public_api} | "
                f"`{_cell(p.api)}` | {patterns} | {p.robots} | {p.generic} | {verified} | "
                f"{_cell(p.notes)} |"
            )
        lines.append("")

    missing = sorted(p.name for p in todo if len(p.examples) < 2)
    lines += [
        "## Research gaps",
        "",
        f"{len(missing)} todo platforms have fewer than 2 verified example boards: "
        + ", ".join(missing)
        + ".",
        "",
        '"Verified" means the endpoint and examples answered a live request on that date; '
        '"no" means the entry comes from documentation or prior knowledge and must be '
        "re-checked before implementation. "
        '"Generic" = whether the generic sitemap/RSS + schema.org JobPosting approach worked '
        "on the evaluated boards (`scripts/evaluate_generic.py`).",
        "",
    ]
    if blocked:
        lines += _render_blocked(blocked)
    return "\n".join(lines)


async def _crawl(platforms: list[Platform], results: Results, only: set[str] | None) -> None:
    async with HttpClient.from_settings() as http:
        await crawl_done_platforms(platforms, results, http, only=only)


def run(
    *,
    crawl: bool = True,
    only: set[str] | None = None,
    platforms_file: Path = PLATFORMS_FILE,
    results_file: Path = RESULTS_FILE,
    report_file: Path = REPORT_FILE,
    blocked_file: Path = BLOCKED_FILE,
) -> str:
    """Load the plan, optionally live-crawl done platforms, write results + report."""
    platforms = load_platforms(platforms_file)
    blocked = load_blocked(blocked_file)
    results = Results.load(results_file)
    if crawl:
        asyncio.run(_crawl(platforms, results, only))
        results.save(results_file)
    report = render_report(platforms, results, datetime.now(UTC), blocked)
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(report, encoding="utf-8")
    return report
