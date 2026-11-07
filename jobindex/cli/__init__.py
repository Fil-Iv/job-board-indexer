"""Command-line interface: ``python -m jobindex.cli <command>``.

Commands:
    add-source <url> [--company-name NAME]   register a job board (hosted board URL,
                                             embed URL or custom-domain careers page)
    crawl <source_id> | --all                crawl one source, or every active one
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.config import get_settings
from jobindex.connectors.registry import Registry, default_registry
from jobindex.crawler import CrawlResult, active_source_ids, crawl_sources
from jobindex.db import create_engine, create_session_factory
from jobindex.http import FetchError, HttpClient
from jobindex.models import FetchStatus, Source


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m jobindex.cli", description="JobIndex command-line interface."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add-source", help="register a job board by URL")
    add.add_argument("url")
    add.add_argument("--company-name", help="override the company name")

    crawl = sub.add_parser("crawl", help="crawl a source now")
    target = crawl.add_mutually_exclusive_group(required=True)
    target.add_argument("source_id", nargs="?", type=int)
    target.add_argument("--all", action="store_true", help="crawl every active source")
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
    return 1 if any(r.status is FetchStatus.FAILED for r in results) else 0


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
        async with HttpClient.from_settings() as http:
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
