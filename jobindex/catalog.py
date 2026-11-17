"""The platform catalog the website shows: display names, coverage status, example
boards with their last coverage run, and the evidence for blocked platforms.

Built from ``coverage/platforms.yaml``, ``coverage/blocked_by_site.yaml`` and
``coverage/results.json``. Those are not deployed to Vercel, so the catalog is copied into
``jobindex/data/coverage.json``: ``make coverage`` refreshes it after a coverage run,
``make coverage-snapshot`` without crawling. ``tests/test_catalog.py`` fails when it
drifts from the sources. The API and feeds keep platform *keys*; pages show names.

Status shown on ``/coverage``:

* ``live``      connector built, platform fully covered;
* ``partial``   connector built, ``partial`` reason in platforms.yaml (e.g. one live board);
* ``blocked``   ``blocked_by_site.yaml`` has a platform-scope block: the site refuses
  every route we could use (robots.txt or HTTP 401/403/429);
* ``not built`` no connector yet.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

SNAPSHOT_FILE = Path(__file__).resolve().parent / "data" / "coverage.json"
CoverageStatus = Literal["live", "partial", "blocked", "not built"]


@lru_cache(maxsize=1)
def catalog() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    return data


@lru_cache(maxsize=1)
def platform_names() -> dict[str, str]:
    return {p["key"]: p["name"] for p in catalog()["platforms"]}


def platform_name(key: str | None) -> str:
    """Display name of a platform key ("wpjobmanager" -> "WP Job Manager"); the key
    itself if the platform is not in the catalog."""
    if not key:
        return ""
    return platform_names().get(key, key)


def _base_name(blocked_platform: str) -> str:
    """"SmartRecruiters (career pages)" -> "SmartRecruiters"."""
    return blocked_platform.split(" (")[0].strip()


def build_snapshot() -> dict[str, Any]:
    """The catalog from the coverage files (needs the repository checkout)."""
    from jobindex.coverage import Results, load_blocked, load_platforms

    platforms = load_platforms()
    blocked = load_blocked()
    results = Results.load()
    blocks_by_name: dict[str, list[dict[str, Any]]] = {}
    for b in blocked:
        blocks_by_name.setdefault(_base_name(b.platform), []).append(
            {
                "label": b.platform,
                "scope": b.scope,
                "impact": b.impact,
                "evidence": [
                    {
                        "url": e.url,
                        "checked_at": e.checked_at,
                        "http_status": e.http_status,
                        "robots_url": e.robots_url,
                        "robots_rule": e.robots_rule,
                        "policy": e.policy,
                    }
                    for e in b.evidence
                ],
            }
        )
    out = []
    for p in sorted(platforms, key=lambda p: (p.type, p.name.casefold())):
        blocks = blocks_by_name.get(p.name, [])
        status: CoverageStatus
        if any(b["scope"] == "platform" for b in blocks):
            status = "blocked"
        elif p.status != "done":
            status = "not built"
        elif p.partial:
            status = "partial"
        else:
            status = "live"
        examples = []
        for url in p.examples:
            r = results.examples.get(url)
            examples.append(
                {
                    "url": url,
                    "last_status": r.last_status if r else None,
                    "last_run_at": r.last_run_at if r else None,
                    "last_success_at": r.last_success_at if r else None,
                    "last_success_jobs": r.last_success_jobs if r else None,
                }
            )
        out.append(
            {
                "key": p.key,
                "name": p.name,
                "type": p.type,
                "status": status,
                "built": p.status == "done",
                "partial": p.partial,
                "examples": examples,
                "blocks": blocks,
            }
        )
    return {"platforms": out}


def write_snapshot(path: Path = SNAPSHOT_FILE) -> int:
    data = build_snapshot()
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return len(data["platforms"])


def main() -> None:
    print(f"{write_snapshot()} platforms -> {SNAPSHOT_FILE}")


if __name__ == "__main__":
    main()
