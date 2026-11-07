"""Connector registry and URL resolution.

Connector modules register their class with ``@register``::

    @register
    class GreenhouseConnector(BaseConnector):
        platform = "greenhouse"

``default_registry()`` imports every module in ``jobindex.connectors`` once, so adding a
connector is just adding a file.

:meth:`Registry.resolve_url` maps any URL to ``(platform, board_identifier)``: first by
asking every connector's ``detect()``; failing that it downloads the page (custom-domain
careers sites such as ``acme.com/careers``) and looks for platform fingerprints in the
HTML — embed scripts, iframes, links to the hosted board.
"""

import importlib
import logging
import pkgutil
from functools import lru_cache
from typing import NamedTuple

from jobindex.connectors.base import BaseConnector
from jobindex.http import HttpClient

log = logging.getLogger(__name__)

_INFRASTRUCTURE_MODULES = {"base", "registry"}


class UnknownPlatformError(LookupError):
    pass


class ResolvedBoard(NamedTuple):
    platform: str
    board_identifier: str


class Registry:
    def __init__(self) -> None:
        self._connectors: dict[str, type[BaseConnector]] = {}

    def add(self, connector_cls: type[BaseConnector]) -> None:
        platform = connector_cls.platform
        existing = self._connectors.get(platform)
        if existing is not None and existing is not connector_cls:
            raise ValueError(f"platform {platform!r} is already registered by {existing!r}")
        self._connectors[platform] = connector_cls

    def get(self, platform: str) -> type[BaseConnector]:
        try:
            return self._connectors[platform]
        except KeyError:
            raise UnknownPlatformError(f"no connector registered for {platform!r}") from None

    @property
    def platforms(self) -> list[str]:
        return sorted(self._connectors)

    def detect(self, url: str) -> ResolvedBoard | None:
        """Resolve from the URL alone (no network)."""
        for connector_cls in self._connectors.values():
            identifier = connector_cls.detect(url)
            if identifier:
                return ResolvedBoard(connector_cls.platform, identifier)
        return None

    def detect_in_html(self, html: str) -> ResolvedBoard | None:
        """Resolve from page HTML: the board referenced most often wins."""
        best: tuple[int, ResolvedBoard] | None = None
        for connector_cls in self._connectors.values():
            for identifier, count in connector_cls.detect_in_html(html).items():
                if best is None or count > best[0]:
                    best = (count, ResolvedBoard(connector_cls.platform, identifier))
        return best[1] if best else None

    async def resolve_url(self, url: str, http: HttpClient) -> ResolvedBoard | None:
        """Resolve ``url`` to ``(platform, board_identifier)``, fetching the page if needed."""
        board = self.detect(url)
        if board is not None:
            return board
        log.info("no platform URL pattern matched %s; inspecting page HTML", url)
        response = await http.get(url)
        final_url = str(response.url)
        if final_url != url:  # e.g. acme.com/careers -> jobs.lever.co/acme
            board = self.detect(final_url)
            if board is not None:
                return board
        return self.detect_in_html(response.text)


_global_registry = Registry()


def register[C: type[BaseConnector]](connector_cls: C) -> C:
    """Class decorator: add a connector to the global registry."""
    _global_registry.add(connector_cls)
    return connector_cls


@lru_cache
def default_registry() -> Registry:
    """Import all connector modules (triggering ``@register``) and return the registry."""
    import jobindex.connectors as package

    for module in pkgutil.iter_modules(package.__path__):
        if module.name not in _INFRASTRUCTURE_MODULES:
            importlib.import_module(f"{package.__name__}.{module.name}")
    return _global_registry


async def resolve_url(
    url: str, http: HttpClient, registry: Registry | None = None
) -> ResolvedBoard | None:
    """Module-level shortcut for ``default_registry().resolve_url``."""
    return await (registry or default_registry()).resolve_url(url, http)
