"""Check candidate boards live and keep the ones worth indexing in the hosted demo.

Reads coverage/candidate_boards.yaml (platform -> slugs / URLs), resolves each to a board
with the connector's URL pattern (no request), then lists its jobs with the connector's
own ``fetch_jobs`` through the polite HTTP client (robots.txt, User-Agent, 1 request/s
per host). Results go to coverage/candidate_results.json; ``--write`` puts every board
with at least ``--min-jobs`` open jobs into coverage/demo_sources_extra.yaml (registered
by the crawl workflow's seed step next to demo_sources.yaml).

    docker compose run --rm -e HTTP_USER_AGENT="JobIndexBot/0.1 (+https://github.com/...)" \\
        app python scripts/check_boards.py [--new] [--write] [--only greenhouse lever]
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobindex.connectors.registry import default_registry
from jobindex.http import HttpClient, RobotsDisallowedError

ROOT = Path(__file__).resolve().parent.parent
CANDIDATES = ROOT / "coverage" / "candidate_boards.yaml"
RESULTS = ROOT / "coverage" / "candidate_results.json"
EXTRA = ROOT / "coverage" / "demo_sources_extra.yaml"
DEMO = ROOT / "coverage" / "demo_sources.yaml"

URL_FORMS = {
    "greenhouse": "https://job-boards.greenhouse.io/{}",
    "lever": "https://jobs.lever.co/{}",
    "ashby": "https://jobs.ashbyhq.com/{}",
    "workable": "https://apply.workable.com/{}/",
    "recruitee": "https://{}.recruitee.com",
    "personio": "https://{}.jobs.personio.de",
    "teamtailor": "https://{}.teamtailor.com/jobs",
    "smartrecruiters": "https://jobs.smartrecruiters.com/{}",
    "breezy": "https://{}.breezy.hr",
    "workday": "{}",
}
TIMEOUT_SECONDS = 300
PARALLEL = 8

EXTRA_HEADER = """\
# More boards for the hosted demo: tech, cybersecurity and remote-first employers on the
# fast JSON platforms, checked live (robots.txt allowed, public list, open jobs) by
#     python scripts/check_boards.py --write
# from coverage/candidate_boards.yaml. Registered by the crawl workflow's seed step
# together with demo_sources.yaml. `jobs` = open postings when checked.

"""


def candidates(only: set[str] | None) -> list[tuple[str, str]]:
    data = yaml.safe_load(CANDIDATES.read_text(encoding="utf-8"))
    out = []
    for platform, items in data.items():
        if only and platform not in only:
            continue
        for item in items or []:
            out.append((platform, URL_FORMS[platform].format(item)))
    return out


async def check(http: HttpClient, platform: str, url: str) -> dict[str, Any]:
    registry = default_registry()
    result: dict[str, Any] = {"platform": platform, "url": url}
    board = registry.detect(url)
    if board is None or board.platform != platform:
        return {**result, "status": "no_match"}
    result["board"] = board.board_identifier
    connector = registry.get(platform)(http)
    try:
        jobs = await asyncio.wait_for(connector.fetch_jobs(board.board_identifier), TIMEOUT_SECONDS)
    except RobotsDisallowedError as exc:
        return {**result, "status": "blocked", "error": str(exc)[:200]}
    except TimeoutError:
        return {**result, "status": "timeout"}
    except Exception as exc:  # 404 / unknown board / parse failure
        return {**result, "status": "error", "error": f"{type(exc).__name__}: {exc}"[:200]}
    n = len({job.external_id for job in jobs})
    return {**result, "status": "ok" if n else "empty", "jobs": n}


async def run(only: set[str] | None, skip: set[str]) -> list[dict[str, Any]]:
    todo = [(p, u) for p, u in candidates(only) if u not in skip]
    gate = asyncio.Semaphore(PARALLEL)
    done = 0

    async with HttpClient.from_settings() as http:

        async def one(platform: str, url: str) -> dict[str, Any]:
            nonlocal done
            async with gate:
                r = await check(http, platform, url)
            done += 1
            print(f"[{done}/{len(todo)}] {r['status']:8} {r.get('jobs', ''):>5} {url}", flush=True)
            return r

        return list(await asyncio.gather(*(one(p, u) for p, u in todo)))


def known_boards() -> set[tuple[str, str]]:
    registry = default_registry()
    seen = set()
    for path in (DEMO, EXTRA):
        if not path.exists():
            continue
        for entry in (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("sources", []):
            board = registry.detect(entry["url"])
            if board:
                seen.add((board.platform, board.board_identifier.lower()))
    return seen


def write_extra(results: list[dict[str, Any]], min_jobs: int) -> int:
    existing = (yaml.safe_load(EXTRA.read_text(encoding="utf-8")) or {}) if EXTRA.exists() else {}
    sources = list(existing.get("sources", []))
    seen = known_boards()
    added = 0
    for r in sorted(results, key=lambda r: (r["platform"], -r.get("jobs", 0))):
        key = (r["platform"], str(r.get("board", "")).lower())
        if r["status"] != "ok" or r["jobs"] < min_jobs or key in seen:
            continue
        sources.append({"platform": r["platform"], "url": r["url"], "jobs": r["jobs"]})
        seen.add(key)
        added += 1
    body = yaml.safe_dump({"sources": sources}, sort_keys=False, allow_unicode=True, width=200)
    EXTRA.write_text(EXTRA_HEADER + body, encoding="utf-8")
    return added


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--only", nargs="+", metavar="PLATFORM")
    parser.add_argument("--write", action="store_true", help="update demo_sources_extra.yaml")
    parser.add_argument("--min-jobs", type=int, default=5)
    parser.add_argument("--new", action="store_true", help="skip boards already in the results")
    args = parser.parse_args()
    previous = json.loads(RESULTS.read_text()) if RESULTS.exists() else []
    skip = {r["url"] for r in previous} if args.new else set()
    results = asyncio.run(run(set(args.only) if args.only else None, skip))
    merged = {r["url"]: r for r in previous} | {r["url"]: r for r in results}
    RESULTS.write_text(json.dumps(sorted(merged.values(), key=lambda r: r["url"]), indent=1) + "\n")
    ok = [r for r in results if r["status"] == "ok"]
    print(f"\n{len(ok)} of {len(results)} boards ok, {sum(r['jobs'] for r in ok):,} open jobs")
    for status in ("empty", "blocked", "error", "timeout", "no_match"):
        print(f"  {status}: {sum(r['status'] == status for r in results)}")
    if args.write:
        every = list(merged.values())
        print(f"added {write_extra(every, args.min_jobs)} boards to {EXTRA.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
