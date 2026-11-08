"""Matching keys for cross-source deduplication: company, title, location and URLs.

These only decide *which* stored postings are the same opening; they never change what
is stored, so they are independent of ``normalize.PARSER_VERSION``.
"""

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit

from jobindex.models.enums import RemoteType


def fold(text: str) -> str:
    """Lower-case, strip accents and collapse whitespace: " Zürich " -> "zurich"."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.lower().split())


# Legal-form words dropped from company names ("Acme GmbH" == "ACME" == "Acme, Inc.").
_LEGAL_SUFFIXES = frozenset(
    {
        *("inc", "incorporated", "llc", "llp", "lp", "ltd", "limited", "plc"),
        *("corp", "corporation", "co", "company", "holding", "holdings", "group"),
        *("gmbh", "mbh", "ag", "kg", "ug", "se", "sa", "sas", "sarl", "srl", "spa"),
        *("bv", "nv", "oy", "oyj", "ab", "as", "asa", "aps", "pty", "pte"),
    }
)
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def company_key(name: str | None) -> str:
    """``"The Acme Company, Inc."`` -> ``"acme"``. Spaces are removed so that
    ``"Hello Fresh"`` and ``"HelloFresh"`` match. Empty string when unknown."""
    if not name:
        return ""
    words = _NON_ALNUM.sub(" ", fold(name).replace("&", " and ")).split()
    if words and words[0] == "the":
        words = words[1:]
    while len(words) > 1 and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return "".join(words)


# "(m/w/d)", "(f/m/x)", "(H/F)", "(all genders)", "- m/w/d" ... — gender markers in titles.
_GENDER_MARKER = re.compile(
    r"[(\[]?\s*\b(?:[mwfdxh]\s*[/|,]\s*){1,3}[mwfdxh]\b\s*[)\]]?"
    r"|[(\[]\s*(?:all genders?|gn)\s*[)\]]",
    re.IGNORECASE,
)
# Work-arrangement noise some boards append to titles: "Engineer (Remote)", "- Hybrid".
_ARRANGEMENT = re.compile(
    r"[(\[]\s*(?:remote|hybrid|on-?site|full[- ]?time|part[- ]?time)\s*[)\]]"
    r"|\s[-–—|]\s*(?:remote|hybrid|on-?site)\s*$",
    re.IGNORECASE,
)
_TITLE_WORDS = {
    "sr": "senior",
    "snr": "senior",
    "jr": "junior",
    "jnr": "junior",
    "mgr": "manager",
    "eng": "engineer",
    "engr": "engineer",
    "dev": "developer",
    "assoc": "associate",
    "asst": "assistant",
    "vp": "vice president",
}


def title_key(title: str | None) -> str:
    """``"Sr. Backend Engineer (m/w/d) - Remote"`` -> ``"senior backend engineer"``."""
    if not title:
        return ""
    text = _GENDER_MARKER.sub(" ", title)
    text = _ARRANGEMENT.sub(" ", text)
    words = _NON_ALNUM.sub(" ", fold(text).replace("&", " and ")).split()
    return " ".join(_TITLE_WORDS.get(w, w) for w in words)


# Spellings of one city that sources use interchangeably (folded, no spaces).
_CITY_ALIASES = {"newyorkcity": "newyork", "nyc": "newyork"}


def city_key(city: str | None) -> str:
    key = _NON_ALNUM.sub("", fold(city)) if city else ""
    return _CITY_ALIASES.get(key, key)


def locations_compatible(
    a_remote: RemoteType,
    a_city: str | None,
    a_country: str | None,
    b_remote: RemoteType,
    b_city: str | None,
    b_country: str | None,
) -> bool:
    """Could these two locations describe the same opening?

    * remote vs remote: same country, or one side names no country ("Remote" vs
      "Remote, US");
    * remote vs not remote: never;
    * otherwise the city must be equal (folded) and the countries must not contradict;
      a posting without a city only matches another without a city in the same country.

    Deliberately conservative: two cities are two openings.
    """
    a_is_remote = a_remote is RemoteType.REMOTE
    b_is_remote = b_remote is RemoteType.REMOTE
    countries_agree = not a_country or not b_country or a_country == b_country
    if a_is_remote or b_is_remote:
        return a_is_remote and b_is_remote and countries_agree
    if city_key(a_city) != city_key(b_city):
        return False
    if not a_city:  # neither has a city: require the same (possibly unknown) country
        return a_country == b_country
    return countries_agree


# Query parameters that only track where a click came from.
_TRACKING_PARAMS = re.compile(
    r"^(?:utm_.*|gh_src|source|src|ref|referrer|lever-source.*|lever-origin|"
    r"ccuid|mode|iis|iisn|codes|_ga|fbclid|gclid)$",
    re.IGNORECASE,
)


def url_key(url: str | None) -> str:
    """Comparable form of a posting URL: no scheme, ``www.``, fragment, trailing slash or
    tracking parameters; host lower-cased. Identifying parameters (``gh_jid=…``) stay."""
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return ""
    host = (parts.hostname or "").lower().removeprefix("www.")
    if not host:
        return ""
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _TRACKING_PARAMS.match(k)
    )
    path = parts.path.rstrip("/")
    return f"{host}{path}" + (f"?{urlencode(query)}" if query else "")


# Platform URL shapes that name the same posting in different ways. Each maps to a
# ``posting:{platform}:{...}`` key; the job id is part of every key, so two *different*
# postings can never share one.
_POSTING_URLS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Workday: /{lang}/{site}/details/{slug}_{req} and /{site}/job/{location}/{slug}_{req}
    (
        "workday",
        re.compile(
            r"^(?P<host>(?:[a-z0-9-]+\.)?wd\d+\.myworkday(?:jobs|site)\.com)"
            r"(?:/[a-z]{2}-[A-Z]{2})?(?:/recruiting/(?P<tenant>[^/]+))?(?:/[a-z]{2}-[A-Z]{2})?"
            r"/(?P<site>[^/]+)/(?:job/(?:[^/]+/)?|details/)[^/]*?_(?P<id>[A-Za-z0-9-]+)$"
        ),
    ),
    # Lever: jobs.lever.co/{site}/{uuid}[/apply]
    (
        "lever",
        re.compile(
            r"^(?:jobs\.(?:eu\.)?lever\.co)/(?P<site>[^/]+)/(?P<id>[0-9a-f-]{36})(?:/apply)?$"
        ),
    ),
    # Greenhouse: boards. / job-boards. / boards.eu. hosts, /{token}/jobs/{id}
    (
        "greenhouse",
        re.compile(
            r"^(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_app\?for=)?"
            r"(?P<site>[^/?]+)/jobs/(?P<id>\d+)$"
        ),
    ),
    # JazzHR: {company}.applytojob.com/apply/{code}[/{slug}]
    (
        "jazzhr",
        re.compile(r"^(?P<site>[a-z0-9-]+)\.applytojob\.com/apply/(?P<id>[A-Za-z0-9]{6,})"),
    ),
    # iCIMS: {host}.icims.com/jobs/{id}[/{slug}/job]
    ("icims", re.compile(r"^(?P<site>[a-z0-9-]+)\.icims\.com/jobs/(?P<id>\d+)(?:/|$)")),
)


def posting_keys(url: str | None) -> set[str]:
    """Every key under which ``url`` identifies a posting: its ``url_key`` and, for
    platforms with several URL shapes per posting, a platform key
    (``posting:workday:umd.wd1.myworkdayjobs.com/umces/JR105179``)."""
    key = url_key(url)
    if not key:
        return set()
    keys = {key}
    path_only = key.split("?", 1)[0]
    for platform, pattern in _POSTING_URLS:
        match = pattern.match(path_only)
        if match:
            groups = match.groupdict()
            prefix = "/".join(g for g in (groups.get("host"), groups.get("tenant")) if g)
            site = f"{prefix}/{groups['site'].lower()}"
            keys.add(f"posting:{platform}:{site.strip('/')}/{groups['id']}")
            break
    return keys
