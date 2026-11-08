"""Command-line interface: ``python -m jobindex.cli <command>``.

Commands:
    add-source <url> [--company-name NAME]   register a job board (hosted board URL,
                                             embed URL or custom-domain careers page)
    seed-demo [--platform KEY ...]           register every example board of every `done`
                                             platform in coverage/platforms.yaml
    seed-demo --file coverage/demo_sources.yaml
                                             register the boards listed in that file
    crawl <source_id> | --all                crawl one source, or every active one (then dedup)
    set-company <source_id> <name>           set the company name used for the source's jobs
    dedup                                    recompute cross-source duplicates
    fill-rates [--markdown] [--strict]       share of open postings with each field, per
                                             platform (--strict: exit 1 on required gaps)
    db-size [--markdown] [--fail-above-mb N] database and table sizes (exit 1 above N MB)
    prune [--closed-days N] [--changes-days N] [--runs-days N]
                                             delete old closed postings / history / runs
                                             (defaults: *_RETENTION_DAYS settings)
    schedule                                 crawl continuously (the worker process)
"""

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.config import get_settings
from jobindex.connectors.registry import Registry, default_registry
from jobindex.coverage import PLATFORMS_FILE, load_platforms
from jobindex.crawler import CrawlResult, active_source_ids, crawl_sources
from jobindex.db import create_engine, create_session_factory
from jobindex.dedup import run_dedup
from jobindex.http import FetchError, HttpClient
from jobindex.maintenance import db_size, prune, render_db_size
from jobindex.models import FetchStatus, Source
from jobindex.quality import fill_rates, render_markdown, render_text
from jobindex.scheduler import CrawlScheduler


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m jobindex.cli", description="JobIndex command-line interface."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add-source", help="register a job board by URL")
    add.add_argument("url")
    add.add_argument("--company-name", help="override the company name")

    seed = sub.add_parser(
        "seed-demo", help="register the example boards from coverage/platforms.yaml"
    )
    seed.add_argument("--platform", nargs="+", metavar="KEY", help="only these platform keys")
    seed.add_argument(
        "--file",
        type=Path,
        help="register the boards of this file instead (e.g. coverage/demo_sources.yaml)",
    )

    company = sub.add_parser("set-company", help="set a source's company name")
    company.add_argument("source_id", type=int)
    company.add_argument("name")

    crawl = sub.add_parser("crawl", help="crawl a source now")
    target = crawl.add_mutually_exclusive_group(required=True)
    target.add_argument("source_id", nargs="?", type=int)
    target.add_argument("--all", action="store_true", help="crawl every active source")

    sub.add_parser("dedup", help="recompute cross-source duplicates (canonical_job_id)")
    fill = sub.add_parser("fill-rates", help="field fill rates of open postings per platform")
    fill.add_argument("--markdown", action="store_true", help="print a Markdown table")
    fill.add_argument("--strict", action="store_true", help="exit 1 if a required field has gaps")
    size = sub.add_parser("db-size", help="database and table sizes")
    size.add_argument("--markdown", action="store_true", help="print a Markdown table")
    size.add_argument(
        "--fail-above-mb", type=float, metavar="MB", help="exit 1 if the database is larger"
    )
    prune_cmd = sub.add_parser("prune", help="delete old closed postings, history and runs")
    prune_cmd.add_argument("--closed-days", type=int, metavar="N", help="closed > N days ago")
    prune_cmd.add_argument("--changes-days", type=int, metavar="N", help="job_changes > N days")
    prune_cmd.add_argument("--runs-days", type=int, metavar="N", help="fetch_runs > N days")
    sub.add_parser("schedule", help="crawl every active source on its interval, forever")
    return parser


async def add_source(
    session_factory: async_sessionmaker[AsyncSession],
    registry: Registry,
    http: HttpClient,
    url: str,
    company_name: str | None,
) -> int:
    try:
        board = await registry.resolve_url(url, http)
    except FetchError as exc:
        print(f"error: could not inspect {url}: {exc}", file=sys.stderr)
        return 2
    if board is None:
        supported = ", ".join(registry.platforms) or "none yet"
        print(
            f"error: no supported platform found for {url!r} (supported: {supported})",
            file=sys.stderr,
        )
        return 2

    async with session_factory() as session:
        existing = await session.scalar(
            select(Source).where(
                Source.platform == board.platform,
                Source.board_identifier == board.board_identifier,
            )
        )
        if existing is not None:
            print(
                f"source already exists: id={existing.id} "
                f"({existing.platform}/{existing.board_identifier})"
            )
            if company_name and not existing.company_name:
                existing.company_name = company_name
                await session.commit()
                print(f"  company set to {company_name!r}")
            return 0

        connector_cls = registry.get(board.platform)
        if company_name is None:
            try:
                company_name = await connector_cls(http).fetch_company_name(board.board_identifier)
            except FetchError:
                company_name = None  # optional nicety; never block adding a source
        source = Source(
            platform=board.platform,
            board_identifier=board.board_identifier,
            base_url=connector_cls.board_url(board.board_identifier),
            company_name=company_name,
            active=True,
        )
        session.add(source)
        await session.commit()
        suffix = f" company={company_name!r}" if company_name else " (set --company-name)"
        print(f"added source id={source.id} ({source.platform}/{source.board_identifier}){suffix}")
        return 0


async def seed_demo(
    session_factory: async_sessionmaker[AsyncSession],
    registry: Registry,
    http: HttpClient,
    only: set[str] | None = None,
    platforms_file: Path = PLATFORMS_FILE,
) -> int:
    """Register every example board of every ``done`` platform (idempotent).

    Gives a fresh install a realistic index: two or more real employers per platform.
    Returns 1 if any example could not be registered (the others are still added).
    """
    failed = 0
    for platform in load_platforms(platforms_file):
        if platform.status != "done" or (only and platform.key not in only):
            continue
        companies = dict(platform.example_companies)
        for url in platform.examples:
            if await add_source(session_factory, registry, http, url, companies.get(url)) != 0:
                failed += 1
    if failed:
        print(f"seed-demo: {failed} example(s) could not be registered", file=sys.stderr)
    return 1 if failed else 0


def load_demo_sources(path: Path) -> list[tuple[str, str | None]]:
    """``(url, company)`` pairs from a demo sources file (format: see
    coverage/demo_sources.yaml)."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"{path}: expected a top-level 'sources:' list")
    result: list[tuple[str, str | None]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("url"), str):
            raise ValueError(f"{path}: every source needs a 'url': {entry!r}")
        company = entry.get("company")
        result.append((entry["url"], str(company) if company else None))
    return result


async def seed_from_file(
    session_factory: async_sessionmaker[AsyncSession],
    registry: Registry,
    http: HttpClient,
    path: Path,
) -> int:
    """Register every board listed in ``path`` (idempotent). Returns 1 if any failed."""
    try:
        entries = load_demo_sources(path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    failed = 0
    for url, company in entries:
        if await add_source(session_factory, registry, http, url, company) != 0:
            failed += 1
    if failed:
        print(f"seed-demo: {failed} of {len(entries)} source(s) failed", file=sys.stderr)
    return 1 if failed else 0


async def report_db_size(
    session_factory: async_sessionmaker[AsyncSession],
    markdown: bool,
    fail_above_mb: float | None,
) -> int:
    async with session_factory() as session:
        size = await db_size(session)
    print(render_db_size(size, markdown=markdown), end="")
    if fail_above_mb is not None and size.database_mb > fail_above_mb:
        print(
            f"error: database is {size.database_mb:.1f} MB, above the {fail_above_mb:g} MB "
            "limit (see docs/OPERATIONS.md#size-budget)",
            file=sys.stderr,
        )
        return 1
    return 0


async def run_prune(
    session_factory: async_sessionmaker[AsyncSession],
    closed_days: int | None,
    changes_days: int | None,
    runs_days: int | None,
) -> int:
    settings = get_settings()
    async with session_factory() as session:
        result = await prune(
            session,
            closed_job_days=closed_days
            if closed_days is not None
            else settings.closed_job_retention_days,
            job_change_days=changes_days
            if changes_days is not None
            else settings.job_change_retention_days,
            fetch_run_days=runs_days
            if runs_days is not None
            else settings.fetch_run_retention_days,
        )
    print(
        f"prune: deleted {result.jobs} closed postings, {result.job_changes} job_changes, "
        f"{result.fetch_runs} fetch_runs"
    )
    return 0


async def set_company(
    session_factory: async_sessionmaker[AsyncSession], source_id: int, name: str
) -> int:
    """Set ``sources.company_name``. Jobs whose platform gives no company name pick it up
    on the next crawl; those changes are recorded with ``cause = 'local'``."""
    name = name.strip()
    if not name:
        print("error: company name must not be empty", file=sys.stderr)
        return 2
    async with session_factory() as session:
        source = await session.get(Source, source_id)
        if source is None:
            print(f"error: source {source_id} does not exist", file=sys.stderr)
            return 2
        old = source.company_name
        source.company_name = name
        await session.commit()
    print(
        f"source {source_id} ({source.platform}/{source.board_identifier}): company "
        f"{old!r} -> {name!r}; applied to its jobs on the next crawl"
    )
    return 0


async def crawl(
    session_factory: async_sessionmaker[AsyncSession],
    registry: Registry,
    http: HttpClient,
    source_id: int | None,
) -> int:
    async with session_factory() as session:
        if source_id is None:
            source_ids = await active_source_ids(session)
        else:
            if await session.get(Source, source_id) is None:
                print(f"error: source {source_id} does not exist", file=sys.stderr)
                return 2
            source_ids = [source_id]
    if not source_ids:
        print("no active sources to crawl")
        return 0

    results = await crawl_sources(
        source_ids,
        session_factory=session_factory,
        http=http,
        concurrency=get_settings().crawl_concurrency,
        registry=registry,
    )
    for result in results:
        print(_format_result(result))
    if any(r.status is FetchStatus.SUCCESS for r in results):
        await dedup(session_factory)
    return 1 if any(r.status is FetchStatus.FAILED for r in results) else 0


async def dedup(session_factory: async_sessionmaker[AsyncSession]) -> int:
    window = timedelta(days=get_settings().dedup_date_window_days)
    async with session_factory() as session:
        result = await run_dedup(session, window=window)
    print(
        f"dedup: {result.postings} postings -> {result.openings} openings "
        f"({result.updated} updated)"
    )
    return 0


async def report_fill_rates(
    session_factory: async_sessionmaker[AsyncSession], markdown: bool, strict: bool
) -> int:
    async with session_factory() as session:
        rows = await fill_rates(session)
    print(render_markdown(rows) if markdown else render_text(rows), end="")
    return 1 if strict and any(r.gaps() for r in rows) else 0


async def schedule(
    session_factory: async_sessionmaker[AsyncSession], registry: Registry, http: HttpClient
) -> int:
    """Run the crawl scheduler until SIGINT/SIGTERM."""
    scheduler = CrawlScheduler(session_factory, http, get_settings(), registry)
    await scheduler.recover_abandoned_runs()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    scheduler.start()
    print("scheduler started; Ctrl+C to stop", flush=True)
    await stop.wait()
    print("stopping scheduler", flush=True)
    await scheduler.stop()
    return 0


def _format_result(r: CrawlResult) -> str:
    if r.status is FetchStatus.FAILED:
        return f"source {r.source_id}: FAILED (run {r.fetch_run_id}): {r.error}"
    return (
        f"source {r.source_id}: ok (run {r.fetch_run_id}) found={r.jobs_found} "
        f"new={r.jobs_new} updated={r.jobs_updated} closed={r.jobs_closed}"
    )


async def _run(args: argparse.Namespace, registry: Registry) -> int:
    engine = create_engine()
    try:
        session_factory = create_session_factory(engine)
        if args.command == "set-company":
            return await set_company(session_factory, args.source_id, args.name)
        if args.command == "dedup":
            return await dedup(session_factory)
        if args.command == "fill-rates":
            return await report_fill_rates(session_factory, args.markdown, args.strict)
        if args.command == "db-size":
            return await report_db_size(session_factory, args.markdown, args.fail_above_mb)
        if args.command == "prune":
            return await run_prune(
                session_factory, args.closed_days, args.changes_days, args.runs_days
            )
        async with HttpClient.from_settings() as http:
            if args.command == "schedule":
                return await schedule(session_factory, registry, http)
            if args.command == "seed-demo" and args.file:
                return await seed_from_file(session_factory, registry, http, args.file)
            if args.command == "seed-demo":
                only = set(args.platform) if args.platform else None
                return await seed_demo(session_factory, registry, http, only)
            if args.command == "add-source":
                return await add_source(
                    session_factory, registry, http, args.url, args.company_name
                )
            return await crawl(
                session_factory, registry, http, None if args.all else args.source_id
            )
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None, registry: Registry | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=get_settings().log_level)
    return asyncio.run(_run(args, registry or default_registry()))
