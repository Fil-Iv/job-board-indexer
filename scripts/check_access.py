"""Check robots.txt + HTTP access for URLs and report what job data they serve.

Never fetches a URL that robots.txt disallows (it prints the deciding rule instead).

Usage:
    python scripts/check_access.py URL [URL ...]          # human-readable
    python scripts/check_access.py --json URL [URL ...]   # one JSON object per line
    python scripts/check_access.py --file urls.txt        # one URL per line ("POST url {json}" ok)
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from jobindex.access import AccessCheck, check_access
from jobindex.http import HttpClient


def _parse_line(line: str) -> tuple[str, str, Any]:
    parts = line.split(maxsplit=2)
    if parts[0].upper() == "POST":
        return "POST", parts[1], json.loads(parts[2]) if len(parts) > 2 else {}
    return "GET", parts[0], None


def _human(c: AccessCheck) -> str:
    robots = "allowed" if c.robots_allowed else "DISALLOWED"
    rule = f" by '{c.robots_rule}'" if c.robots_rule else ""
    status = c.http_status if c.http_status is not None else "-"
    return (
        f"{c.method} {c.url}\n  robots {c.robots_url} -> HTTP {c.robots_http_status}: "
        f"{robots}{rule}\n  fetch -> {status} {c.content_type or ''} {c.size or ''} "
        f"{c.error or ''}\n  signals {c.signals}"
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("urls", nargs="*")
    parser.add_argument("--file")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    lines = list(args.urls)
    if args.file:
        text = await asyncio.to_thread(Path(args.file).read_text, encoding="utf-8")
        lines += [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    async with HttpClient.from_settings() as http:

        async def one(line: str) -> AccessCheck:
            method, url, body = _parse_line(line)
            return await check_access(url, http, method=method, json_body=body)

        for check in await asyncio.gather(*(one(line) for line in lines)):
            print(json.dumps(check.as_dict()) if args.json else _human(check), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
