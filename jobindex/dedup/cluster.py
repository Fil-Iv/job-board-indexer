"""Grouping postings into openings (pure logic, no database).

Two postings are the same opening when they come from **different sources** and either

1. **strong signal** — they share an apply URL or source URL (after ``url_key``), e.g. a
   job board that re-lists an ATS posting and links to it; or
2. **fuzzy match** — same ``company_key``, same ``title_key``, compatible locations
   (``locations_compatible``: same city, or both remote) and close in time
   (``dates_compatible``).

Postings from the *same* source are never merged: a board listing two postings means
two openings (e.g. one per city, or two head-count requisitions). Groups are built with
union-find; a merge is refused if it would put two postings of one source together, and a
fuzzy merge is refused unless every pair across the two groups is location- and
date-compatible (no chaining "Remote" -> "Remote, US" -> "Remote, CA").
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from jobindex.dedup.keys import company_key, locations_compatible, title_key, url_key
from jobindex.models.enums import JobStatus, RemoteType

DEFAULT_DATE_WINDOW = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class Posting:
    """The columns of ``jobs`` that dedup looks at."""

    id: int
    source_id: int
    company: str | None
    title: str
    city: str | None
    country: str | None
    remote_type: RemoteType
    posted_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime
    status: JobStatus
    apply_url: str | None = None
    source_url: str | None = None


def dates_compatible(a: Posting, b: Posting, window: timedelta = DEFAULT_DATE_WINDOW) -> bool:
    """Were these posted at about the same time?

    With both ``posted_at`` known: at most ``window`` apart. Otherwise (many platforms
    publish no date) the time each was live in our index — first seen to last seen — must
    overlap, give or take ``window``. A re-post months after the old one closed is a new
    opening either way.
    """
    if a.posted_at and b.posted_at:
        return abs(a.posted_at - b.posted_at) <= window
    a_start, b_start = a.posted_at or a.first_seen_at, b.posted_at or b.first_seen_at
    return a_start <= b.last_seen_at + window and b_start <= a.last_seen_at + window


def _compatible(a: Posting, b: Posting, window: timedelta) -> bool:
    return dates_compatible(a, b, window) and locations_compatible(
        a.remote_type, a.city, a.country, b.remote_type, b.city, b.country
    )


class _Groups:
    """Union-find over postings that keeps each group's members."""

    def __init__(self, postings: list[Posting]) -> None:
        self.parent = {p.id: p.id for p in postings}
        self.members: dict[int, list[Posting]] = {p.id: [p] for p in postings}

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int, *, check_window: timedelta | None = None) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return True
        ma, mb = self.members[ra], self.members[rb]
        if {p.source_id for p in ma} & {p.source_id for p in mb}:
            return False  # never two postings of the same source in one opening
        if check_window is not None and not all(
            _compatible(x, y, check_window) for x in ma for y in mb
        ):
            return False
        if len(ma) < len(mb):
            ra, rb, ma, mb = rb, ra, mb, ma
        self.parent[rb] = ra
        ma.extend(mb)
        del self.members[rb]
        return True


def canonical_of(members: Iterable[Posting]) -> Posting:
    """The posting that represents the opening: an open one if any, then the one we saw
    first (then lowest id, for determinism)."""
    return min(members, key=lambda p: (p.status is not JobStatus.OPEN, p.first_seen_at, p.id))


def cluster(postings: Iterable[Posting], window: timedelta = DEFAULT_DATE_WINDOW) -> dict[int, int]:
    """Return ``{posting id: canonical posting id}`` for every posting (singletons map to
    themselves)."""
    items = sorted(postings, key=lambda p: p.id)
    groups = _Groups(items)

    # 1. Strong signal: identical apply/source URL across sources. A URL that a single
    #    source uses for several postings (a generic "apply here" page) proves nothing.
    by_url: dict[str, list[Posting]] = defaultdict(list)
    for p in items:
        for key in {url_key(p.apply_url), url_key(p.source_url)} - {""}:
            by_url[key].append(p)
    for same_url in by_url.values():
        per_source: dict[int, int] = defaultdict(int)
        for p in same_url:
            per_source[p.source_id] += 1
        unique = [p for p in same_url if per_source[p.source_id] == 1]
        for p in unique[1:]:
            groups.union(unique[0].id, p.id)

    # 2. Fuzzy: same company + title, then location and date checks on every pair.
    blocks: dict[tuple[str, str], list[Posting]] = defaultdict(list)
    for p in items:
        company, title = company_key(p.company), title_key(p.title)
        if company and title:
            blocks[company, title].append(p)
    for block in blocks.values():
        if len({p.source_id for p in block}) < 2:
            continue
        block.sort(key=lambda p: (p.posted_at or p.first_seen_at, p.id))
        for i, a in enumerate(block):
            for b in block[i + 1 :]:
                if a.source_id != b.source_id and _compatible(a, b, window):
                    groups.union(a.id, b.id, check_window=window)

    canonical: dict[int, int] = {}
    for members in groups.members.values():
        head = canonical_of(members).id
        for p in members:
            canonical[p.id] = head
    return canonical
