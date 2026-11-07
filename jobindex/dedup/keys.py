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


def city_key(city: str | None) -> str:
    return _NON_ALNUM.sub("", fold(city)) if city else ""


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
