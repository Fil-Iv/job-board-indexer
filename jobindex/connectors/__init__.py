"""Platform connectors. One module per platform: ``jobindex/connectors/<platform>.py``.

See ``base.py`` for the interface and contract, ``registry.py`` for registration and
URL resolution.
"""

from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import (
    Registry,
    ResolvedBoard,
    UnknownPlatformError,
    default_registry,
    register,
    resolve_url,
)

__all__ = [
    "BaseConnector",
    "ConnectorError",
    "RawJob",
    "Registry",
    "ResolvedBoard",
    "UnknownPlatformError",
    "default_registry",
    "register",
    "resolve_url",
]
