"""Regenerate docs/COVERAGE.md from coverage/platforms.yaml.

Live-crawls the example boards of every `done` platform (job list only, polite HTTP
client), stores the outcome in coverage/results.json and rewrites docs/COVERAGE.md.

Usage:
    python scripts/coverage_report.py                 # crawl + render
    python scripts/coverage_report.py --no-crawl      # render from stored results
    python scripts/coverage_report.py --only lever    # crawl only some platforms
    make coverage
"""

import argparse
import logging
import sys

from jobindex.coverage import REPORT_FILE, PlanError, run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--no-crawl", action="store_true", help="only re-render the report")
    parser.add_argument("--only", nargs="+", metavar="KEY", help="crawl only these platform keys")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        run(crawl=not args.no_crawl, only=set(args.only) if args.only else None)
    except PlanError as exc:
        print(f"coverage/platforms.yaml is invalid: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {REPORT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
