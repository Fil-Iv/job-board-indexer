from enum import StrEnum


class RemoteType(StrEnum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"
    UNKNOWN = "unknown"


class JobStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class ChangeCause(StrEnum):
    """Why a job_changes row exists (see jobindex/crawler.py)."""

    UPSTREAM = "upstream"  # the posting itself changed at the source
    PARSER = "parser"  # same source data, re-normalised by a newer normalize.PARSER_VERSION
    LOCAL = "local"  # same source data and parser, our own metadata changed (e.g. set-company)


class FetchStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


def enum_values(enum_cls: type[StrEnum]) -> list[str]:
    """Store enum *values* (``"open"``) in the database rather than member names (``"OPEN"``)."""
    return [member.value for member in enum_cls]
