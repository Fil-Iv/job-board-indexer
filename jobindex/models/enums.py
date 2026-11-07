from enum import StrEnum


class RemoteType(StrEnum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"
    UNKNOWN = "unknown"


class JobStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class FetchStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


def enum_values(enum_cls: type[StrEnum]) -> list[str]:
    """Store enum *values* (``"open"``) in the database rather than member names (``"OPEN"``)."""
    return [member.value for member in enum_cls]
