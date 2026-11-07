"""A small robots.txt parser following RFC 9309.

Python's ``urllib.robotparser`` is not used because it ignores ``*`` / ``$`` wildcards
and resolves conflicts by first match instead of longest match, which can wrongly
allow paths a site disallows.

Semantics implemented:

* Groups are selected by the most specific matching product token (case-insensitive),
  falling back to ``*``. Several groups for the same agent are merged.
* The longest matching rule wins; on a tie ``Allow`` wins. No matching rule => allowed.
* ``*`` matches any sequence of characters, a trailing ``$`` anchors the end.
* ``Crawl-delay`` (non-standard but widely used) is exposed for the matching group.
"""

import contextlib
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit


@dataclass(slots=True)
class _Rule:
    allow: bool
    path: str
    pattern: re.Pattern[str]


@dataclass(slots=True)
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[_Rule] = field(default_factory=list)
    crawl_delay: float | None = None


def _compile(path: str) -> re.Pattern[str]:
    anchored = path.endswith("$")
    if anchored:
        path = path[:-1]
    regex = ".*".join(re.escape(part) for part in path.split("*"))
    return re.compile(regex + ("$" if anchored else ""))


def _normalise_path(path: str) -> str:
    # Compare decoded forms so "/a%2Fb" and "/a/b"-style encodings match consistently.
    return unquote(path)


class RobotsTxt:
    def __init__(self, groups: list[_Group]) -> None:
        self._groups = groups

    @classmethod
    def allow_all(cls) -> "RobotsTxt":
        return cls([])

    @classmethod
    def disallow_all(cls) -> "RobotsTxt":
        return cls([_Group(agents=["*"], rules=[_Rule(False, "/", _compile("/"))])])

    @classmethod
    def parse(cls, text: str) -> "RobotsTxt":
        groups: list[_Group] = []
        current: _Group | None = None
        last_was_agent = False
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if current is None or not last_was_agent:
                    current = _Group()
                    groups.append(current)
                current.agents.append(value.lower())
                last_was_agent = True
                continue
            last_was_agent = False
            if current is None:
                continue  # rules before any user-agent line are ignored
            if key in ("allow", "disallow"):
                if not value:
                    continue  # "Disallow:" with no path means allow everything
                path = _normalise_path(value)
                current.rules.append(_Rule(key == "allow", path, _compile(path)))
            elif key == "crawl-delay":
                with contextlib.suppress(ValueError):
                    current.crawl_delay = float(value)
        return cls(groups)

    def _groups_for(self, user_agent: str) -> list[_Group]:
        # Product token = the part before "/" (e.g. "JobIndexBot" in "JobIndexBot/0.1 (...)").
        token = user_agent.split("/", 1)[0].strip().lower()
        specific = [g for g in self._groups if any(a != "*" and a == token for a in g.agents)]
        if specific:
            return specific
        return [g for g in self._groups if "*" in g.agents]

    def can_fetch(self, user_agent: str, url: str) -> bool:
        parts = urlsplit(url)
        target = _normalise_path((parts.path or "/") + (f"?{parts.query}" if parts.query else ""))
        best: _Rule | None = None
        for group in self._groups_for(user_agent):
            for rule in group.rules:
                if not rule.pattern.match(target):
                    continue
                if (
                    best is None
                    or len(rule.path) > len(best.path)
                    or (len(rule.path) == len(best.path) and rule.allow and not best.allow)
                ):
                    best = rule
        return best is None or best.allow

    def crawl_delay(self, user_agent: str) -> float | None:
        delays = [g.crawl_delay for g in self._groups_for(user_agent) if g.crawl_delay is not None]
        return max(delays) if delays else None
