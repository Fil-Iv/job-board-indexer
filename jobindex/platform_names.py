"""Display names of platforms for the UI: ``"wpjobmanager"`` -> ``"WP Job Manager"``.

The names come from ``coverage/platforms.yaml``, which is not deployed to Vercel, so they
are copied into ``jobindex/data/platform_names.json``; ``tests/test_platform_names.py``
fails when the two drift apart. Regenerate with ``make platform-names``. The API and
feeds keep the platform *keys*; only pages show names.
"""

import json
from functools import lru_cache
from pathlib import Path

NAMES_FILE = Path(__file__).resolve().parent / "data" / "platform_names.json"


@lru_cache(maxsize=1)
def platform_names() -> dict[str, str]:
    names: dict[str, str] = json.loads(NAMES_FILE.read_text(encoding="utf-8"))
    return names


def platform_name(key: str | None) -> str:
    """Display name of a platform key; the key itself if the platform is not listed."""
    if not key:
        return ""
    return platform_names().get(key, key)


def build_names() -> dict[str, str]:
    """key -> name for every platform in coverage/platforms.yaml (needs the repo)."""
    from jobindex.coverage import load_platforms

    return {p.key: p.name for p in sorted(load_platforms(), key=lambda p: p.key)}


def main() -> None:
    names = build_names()
    NAMES_FILE.write_text(json.dumps(names, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(names)} platform names -> {NAMES_FILE}")


if __name__ == "__main__":
    main()
