"""Per-client sliding-window rate limits (in memory, per process).

On Vercel each function instance has its own window, so this is a brake on bursts, not
an exact quota; ``pending_sources`` also has a global hourly cap in the database.
"""

import time
from collections import deque

from fastapi import Request


class RateLimiter:
    """Sliding window: at most ``limit`` requests per ``window`` seconds per key."""

    def __init__(self, window: float = 60.0) -> None:
        self.window = window
        self._hits: dict[str, deque[float]] = {}

    def retry_after(self, key: str, limit: int, now: float | None = None) -> int:
        """0 when the request may proceed (and is counted), else seconds to wait."""
        now = time.monotonic() if now is None else now
        if len(self._hits) > 10_000:  # forget idle clients
            self._hits = {k: v for k, v in self._hits.items() if v and v[-1] > now - self.window}
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        if len(hits) >= limit:
            return max(1, int(hits[0] + self.window - now) + 1)
        hits.append(now)
        return 0


limiter = RateLimiter()  # MCP: per minute


def client_ip(request: Request) -> str:
    """Vercel puts the real client first in X-Forwarded-For."""
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "?")
