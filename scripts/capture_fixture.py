"""Save a real response as a test fixture (through the polite HttpClient: robots.txt,
User-Agent, rate limit).

Usage:
    python scripts/capture_fixture.py URL tests/fixtures/<platform>/<name>
    python scripts/capture_fixture.py --post '{"limit": 20}' URL OUT
    python scripts/capture_fixture.py --show URL          # print status + first 2000 chars

JSON responses are re-indented (2 spaces) so fixtures diff cleanly; everything else is
saved byte for byte. Trim fixtures by hand afterwards if they are large, and note the
capture date + URL in the test module docstring.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from jobindex.http import HttpClient


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("url")
    parser.add_argument("out", nargs="?")
    parser.add_argument("--post", help="JSON body: send a POST instead of a GET")
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()
    async with HttpClient.from_settings() as http:
        body = json.loads(args.post) if args.post else None
        response = await http.request("POST" if body is not None else "GET", args.url, json=body)
    content = response.content
    if "json" in response.headers.get("content-type", ""):
        content = (json.dumps(response.json(), indent=2, ensure_ascii=False) + "\n").encode()
    if args.show or not args.out:
        ctype = response.headers.get("content-type")
        print(f"{response.status_code} {ctype} {len(content)}b {response.url}")
        print(content[:2000].decode("utf-8", "replace"))
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(out.write_bytes, content)
        print(f"saved {len(content)} bytes to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
