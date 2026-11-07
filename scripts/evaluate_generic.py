"""Evaluate the generic (sitemap/RSS + schema.org JobPosting) approach on real boards.

For each board URL: discover job URLs from sitemaps (robots.txt + /sitemap.xml) and RSS,
fetch a small sample of job pages and check them for a JobPosting with the fields we
need. Only reads what robots.txt allows.

Usage:
    python scripts/evaluate_generic.py URL [URL ...]
    python scripts/evaluate_generic.py --file boards.txt   # "platform URL" per line
    python scripts/evaluate_generic.py --json ...          # one JSON object per line
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from jobindex.discovery import evaluate_board
from jobindex.http import HttpClient


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("urls", nargs="*")
    parser.add_argument("--file")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    boards: list[tuple[str, str]] = [("-", u) for u in args.urls]
    if args.file:
        text = await asyncio.to_thread(Path(args.file).read_text, encoding="utf-8")
        for line in text.splitlines():
            if line.strip() and not line.startswith("#"):
                platform, url = line.split(maxsplit=1)
                boards.append((platform, url.strip()))
    async with HttpClient.from_settings() as http:

        async def one(platform: str, url: str) -> None:
            try:
                ev = await evaluate_board(http, url)
            except Exception as exc:  # one broken board must not stop the evaluation
                error = f"{type(exc).__name__}: {exc}"[:300]
                print(
                    json.dumps({"platform": platform, "board_url": url, "error": error})
                    if args.json
                    else f"{platform:16} ERR {url}: {error}",
                    flush=True,
                )
                return
            if args.json:
                print(json.dumps({"platform": platform, **ev.as_dict()}), flush=True)
            else:
                print(
                    f"{platform:16} {'OK ' if ev.generic_ok else 'no '} {url}\n"
                    f"   sitemaps={ev.sitemaps} feeds={ev.feeds} urls={ev.urls_found} "
                    f"job_urls={ev.job_urls} sampled={ev.sampled} "
                    f"jobposting={ev.with_jobposting} fields={ev.field_coverage} "
                    f"blocked={len(ev.blocked)}",
                    flush=True,
                )

        await asyncio.gather(*(one(p, u) for p, u in boards))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
