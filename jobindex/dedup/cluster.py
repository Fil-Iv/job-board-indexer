"""Grouping postings into openings (pure logic, no database).

Two postings are the same opening when they come from **different sources** and either

1. **strong signal** — they share an apply URL or source URL (after ``posting_keys``:
   ``url_key`` plus platform keys, so Workday's ``/details/`` and ``/job/`` URLs of one
   requisition match), e.g. a job board that re-lists an ATS posting and links to it; or
   one posting's description links to exactly one posting of another source (a job
   board ad saying "apply at umd.wd1.myworkdayjobs.com/...JR105179"); or
2. **fuzzy match** — same ``company_key``, same ``title_key``, compatible locations
   (``locations_compatible``: same city, or both remote), close in time
   (``dates_compatible``) and, when both have a description, mostly the same words
   (``MIN_DESCRIPTION_OVERLAP``).

Postings from the *same* source are never merged: a board listing two postings means
two openings (e.g. one per city, or two head-count requisitions). Groups are built with
union-find; a merge is refused if it would put two postings of one source together, and a
fuzzy merge is refused unless every pair across the two groups is location- and
date-compatible (no chaining "Remote" -> "Remote, US" -> "Remote, CA").
"""

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from jobindex.dedup.keys import company_key, locations_compatible, posting_keys, title_key
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
    #: URLs mentioned in the description (``keys.linked_urls``)
    linked_urls: tuple[str, ...] = ()


def dates_compatible(a: Posting, b: Posting, window: timedelta = DEFAULT_DATE_WINDOW) -> bool:
    """Were these posted at about the same time?

    With both ``posted_at`` known: at most ``window`` apart, or the earlier one was still
    listed when the later one was posted (re-listing of an open requisition). Otherwise
    (many platforms publish no date) the time each was live in our index — first seen
    to last seen — must overlap, give or take ``window``. A re-post months after the old
    one closed is a new opening either way.
    """
    if a.posted_at and b.posted_at:
        if abs(a.posted_at - b.posted_at) <= window:
            return True
        # Further apart: still the same opening if the earlier listing was live when the
        # later one appeared (a job board re-listing a requisition that is still open).
        # A re-post after the original closed is a new opening.
        if a.posted_at <= b.posted_at:
            return a.last_seen_at >= b.posted_at
        return b.last_seen_at >= a.posted_at
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


#: Fuzzy merges of two postings that both have a description need at least this share of
#: the shorter description's words in the other one. Calibrated on real pairs
#: (2026-11-08): true re-listings 0.78-1.0; same generic title, different job ("Lecturer
#: in Discipline" in two departments, two Cell Biology tracks) 0.32-0.49.
MIN_DESCRIPTION_OVERLAP = 0.6

_WORD = re.compile(r"[a-z0-9]{4,}")


def description_words(text: str | None) -> frozenset[str]:
    """Words (4+ letters/digits, lower case) of a description, for :func:`overlap`."""
    return frozenset(_WORD.findall((text or "").lower()))


def overlap(a: frozenset[str], b: frozenset[str]) -> float | None:
    """Share of the smaller word set found in the other; ``None`` if one is empty."""
    if not a or not b:
        return None
    return len(a & b) / min(len(a), len(b))


def fuzzy_candidates(postings: Iterable[Posting]) -> set[int]:
    """Ids of postings that share company + title keys with a posting of another source
    (the only ones whose descriptions the fuzzy rule needs)."""
    blocks: dict[tuple[str, str], list[Posting]] = defaultdict(list)
    for p in postings:
        company, title = company_key(p.company), title_key(p.title)
        if company and title:
            blocks[company, title].append(p)
    return {
        p.id for block in blocks.values() if len({p.source_id for p in block}) > 1 for p in block
    }


def cluster(
    postings: Iterable[Posting],
    window: timedelta = DEFAULT_DATE_WINDOW,
    descriptions: Mapping[int, frozenset[str]] | None = None,
) -> dict[int, int]:
    """Return ``{posting id: canonical posting id}`` for every posting (singletons map to
    themselves). ``descriptions`` (``description_words`` by posting id, at least for
    ``fuzzy_candidates``) enables the description check of fuzzy merges."""
    words = descriptions or {}
    items = sorted(postings, key=lambda p: p.id)
    groups = _Groups(items)

    # 1. Strong signal: identical apply/source URL across sources. A URL that a single
    #    source uses for several postings (a generic "apply here" page) proves nothing.
    by_url: dict[str, list[Posting]] = defaultdict(list)
    for p in items:
        for key in posting_keys(p.apply_url) | posting_keys(p.source_url):
            by_url[key].append(p)
    unique_by_url: dict[str, list[Posting]] = {}
    for key, same_url in by_url.items():
        per_source: dict[int, int] = defaultdict(int)
        for p in same_url:
            per_source[p.source_id] += 1
        unique = [p for p in same_url if per_source[p.source_id] == 1]
        unique_by_url[key] = unique
        for p in unique[1:]:
            groups.union(unique[0].id, p.id)

    # 1b. A description that links to a posting of another source. Only when the links
    #     point at exactly one posting of that source: an ad listing several jobs of one
    #     board ("see all our openings") is not one of them.
    for p in items:
        if not p.linked_urls:
            continue
        targets: dict[int, set[int]] = defaultdict(set)  # source -> linked posting ids
        for url in p.linked_urls:
            for key in posting_keys(url):
                for target in unique_by_url.get(key, ()):
                    if target.source_id != p.source_id:
                        targets[target.source_id].add(target.id)
        for linked in targets.values():
            if len(linked) == 1:
                groups.union(p.id, next(iter(linked)))

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
                    shared = overlap(words.get(a.id, frozenset()), words.get(b.id, frozenset()))
                    if shared is not None and shared < MIN_DESCRIPTION_OVERLAP:
                        continue  # same title, different job
                    groups.union(a.id, b.id, check_window=window)

    canonical: dict[int, int] = {}
    for members in groups.members.values():
        head = canonical_of(members).id
        for p in members:
            canonical[p.id] = head
    return canonical
