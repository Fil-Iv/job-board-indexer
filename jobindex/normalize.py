"""Normalisation helpers shared by connectors and the crawler.

* :func:`html_to_text` — strip HTML to readable plain text.
* :func:`parse_location` — "Toronto, ON, Canada" -> city / region / ISO country code.
* :func:`normalize_country` — "United States", "USA", "us" -> "US".
* :func:`detect_remote_type` — onsite / hybrid / remote / unknown from short text.
* :func:`parse_salary` / :func:`find_salary` — salary strings in many formats/currencies.
* :func:`normalize_employment_type` — "Full-time", "FullTime", "Permanent" -> "full_time".
* :func:`content_hash` — stable hash over the meaningful fields of a job.

All functions are pure and conservative: when unsure they return ``None`` / ``UNKNOWN``
rather than guess.
"""

import hashlib
import html as html_lib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from functools import lru_cache
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pycountry
from selectolax.lexbor import LexborHTMLParser

from jobindex.models.enums import RemoteType

# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_WHITESPACE = re.compile(r"[ \t\r\f\v\u00a0]+")
_BLANK_LINES = re.compile(r"\n{3,}")
# Line breaks go only before/after block-level elements; inline tags (<b>, <a>, <span>)
# must not split sentences.
_BLOCK_TAG = re.compile(
    r"<(/?(?:p|div|br|li|ul|ol|h[1-6]|tr|table|section|article|blockquote|header|footer)\b)",
    re.IGNORECASE,
)


def html_to_text(html: str | None) -> str | None:
    """Convert an HTML fragment to plain text, keeping block structure as line breaks."""
    if not html or not html.strip():
        return None
    tree = LexborHTMLParser(_BLOCK_TAG.sub(r"\n<\1", html))
    for node in tree.css("script, style, noscript"):
        node.decompose()
    root = tree.body or tree.root
    if root is None:
        return None
    text = _WHITESPACE.sub(" ", root.text(separator=""))
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    return _BLANK_LINES.sub("\n\n", text).strip() or None


def unescape_html(text: str | None) -> str | None:
    """Undo HTML-entity escaping (Greenhouse returns ``&lt;p&gt;`` instead of ``<p>``)."""
    return html_lib.unescape(text) if text else text


# ---------------------------------------------------------------------------
# URLs & dates
# ---------------------------------------------------------------------------


def split_url(url: str) -> tuple[str, list[str], dict[str, list[str]]]:
    """``"https://Boards.x.io/acme/jobs/1?for=y"`` -> ``("boards.x.io", ["acme", "jobs", "1"],
    {"for": ["y"]})``. Accepts URLs without a scheme; path segments are percent-decoded."""
    url = url.strip()
    if "://" not in url:
        url = "https://" + url.lstrip("/")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    segments = [unquote(seg) for seg in parts.path.split("/") if seg]
    return host, segments, parse_qs(parts.query)


def parse_datetime(value: str | int | float | None) -> datetime | None:
    """Parse ISO-8601 strings (with ``Z`` or offsets) and epoch milliseconds to aware UTC."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, int | float):
            return datetime.fromtimestamp(value / 1000, tz=UTC)
        text = value.strip()
        if text.endswith(" UTC"):  # "2026-10-30 16:32:58 UTC" (Recruitee)
            text = text[:-4] + "+00:00"
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Countries & locations
# ---------------------------------------------------------------------------

_COUNTRY_ALIASES = {
    "usa": "US",
    "u.s.": "US",
    "u.s.a.": "US",
    "us": "US",
    "united states of america": "US",
    "america": "US",
    "uk": "GB",
    "u.k.": "GB",
    "england": "GB",
    "scotland": "GB",
    "wales": "GB",
    "northern ireland": "GB",
    "great britain": "GB",
    "britain": "GB",
    "south korea": "KR",
    "korea": "KR",
    "russia": "RU",
    "vietnam": "VN",
    "taiwan": "TW",
    "czech republic": "CZ",
    "czechia": "CZ",
    "turkey": "TR",
    "türkiye": "TR",
    "holland": "NL",
    "the netherlands": "NL",
    "uae": "AE",
    "iran": "IR",
    "bolivia": "BO",
    "venezuela": "VE",
    "tanzania": "TZ",
    "moldova": "MD",
    "laos": "LA",
    "syria": "SY",
    "brasil": "BR",
    "deutschland": "DE",
    "españa": "ES",
    "méxico": "MX",
    "ivory coast": "CI",
}

# Words that describe a region, not a city or country.
_NON_PLACES = {
    "remote",
    "hybrid",
    "onsite",
    "on-site",
    "anywhere",
    "worldwide",
    "global",
    "international",
    "europe",
    "emea",
    "apac",
    "latam",
    "americas",
    "north america",
    "south america",
    "asia",
    "africa",
    "oceania",
    "european union",
    "eu",
    "multiple locations",
    "various",
    "flexible",
    "home based",
    "home-based",
    "distributed",
    "nationwide",
    "in office",
    "office",
    "hq",
    "headquarters",
    "main office",
}

_CITY_ALIASES = {
    "nyc": ("New York", "NY", "US"),
    "new york city": ("New York", "NY", "US"),
    "sf": ("San Francisco", "CA", "US"),
    "san francisco bay area": ("San Francisco", "CA", "US"),
    "bay area": ("San Francisco", "CA", "US"),
    "washington dc": ("Washington", "DC", "US"),
    "washington, d.c.": ("Washington", "DC", "US"),
}


@lru_cache(maxsize=1)
def _country_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for country in pycountry.countries:
        for attr in ("name", "official_name", "common_name", "alpha_3"):
            value = getattr(country, attr, None)
            if value:
                index[value.lower()] = country.alpha_2
        index[country.alpha_2.lower()] = country.alpha_2
    index.update(_COUNTRY_ALIASES)
    return index


@lru_cache(maxsize=1)
def _subdivisions() -> dict[str, dict[str, str]]:
    """{"US": {"ca": "CA", "california": "CA", ...}, "CA": {...}} for regions we normalise."""
    result: dict[str, dict[str, str]] = {}
    for country in ("US", "CA", "AU"):
        mapping: dict[str, str] = {}
        for sub in pycountry.subdivisions.get(country_code=country) or []:  # type: ignore[no-untyped-call]
            code = sub.code.split("-", 1)[1]
            mapping[code.lower()] = code
            mapping[sub.name.lower()] = code
        result[country] = mapping
    result["US"]["d.c."] = result["US"]["dc"] = "DC"
    return result


def normalize_country(value: str | None) -> str | None:
    """Return the ISO 3166-1 alpha-2 code for a country name/code, or ``None``."""
    if not value:
        return None
    key = value.strip().lower()
    index = _country_index()
    return index.get(key) or index.get(key.strip(".")) or index.get(key.replace(".", ""))


def normalize_region(region: str | None, country: str | None) -> str | None:
    """Map US/Canadian/Australian state names to their codes; other regions pass through."""
    if not region:
        return None
    region = region.strip()
    if country in _subdivisions():
        return _subdivisions()[country].get(region.lower(), region)
    return region


@dataclass(frozen=True, slots=True)
class Location:
    city: str | None = None
    region: str | None = None
    country: str | None = None  # ISO 3166-1 alpha-2


_MULTI_SPLIT = re.compile(r"\s*(?:;|\||•|\s·\s|\s/\s|\sor\s|\n)\s*", re.IGNORECASE)
_REMOTE_WORDS = re.compile(
    r"\b(?:fully\s+)?(?:remote|hybrid|on-?site|in[- ]office|wfh)\b\s*[-:–—]?\s*", re.IGNORECASE
)


def _clean_part(part: str) -> str:
    return part.strip().strip("()[]-–—:.").strip()


def parse_location(raw: str | None) -> Location:
    """Best-effort parse of a free-text location. Uses the first location if several."""
    if not raw:
        return Location()
    candidates = [c for c in _MULTI_SPLIT.split(raw) if c.strip()]
    # Prefer a candidate that names a concrete place over "Remote".
    for candidate in candidates or [raw]:
        loc = _parse_single(candidate)
        if loc != Location():
            return loc
    return Location()


def _parse_single(text: str) -> Location:
    text = _REMOTE_WORDS.sub(" ", text)
    text = text.replace("(", ",").replace(")", ",")
    parts = [p for p in (_clean_part(p) for p in text.split(",")) if p]
    parts = [p for p in parts if p.lower() not in _NON_PLACES]
    if not parts:
        return Location()
    # Country-first notation, e.g. Workday's "US, CA, Santa Clara" or "DE, Munich". The
    # leading code is the country, so don't re-guess it (DE must not become Delaware).
    leading_country = (
        normalize_country(parts[0]) if len(parts[0]) == 2 and parts[0].isupper() else None
    )
    trailing_country_name = len(parts[-1]) > 2 and normalize_country(parts[-1])
    if leading_country and len(parts) >= 2 and not trailing_country_name:
        rest = parts[1:]
        regions = _subdivisions().get(leading_country, {})
        if len(rest) == 1 and rest[0].lower() in regions:
            return Location(region=regions[rest[0].lower()], country=leading_country)
        lead_region = normalize_region(rest[0], leading_country) if len(rest) >= 2 else None
        return Location(city=rest[-1], region=lead_region, country=leading_country)

    alias = _CITY_ALIASES.get(", ".join(parts).lower()) or _CITY_ALIASES.get(parts[0].lower())
    if alias:
        return Location(*alias)

    us, ca, au = (_subdivisions()[c] for c in ("US", "CA", "AU"))
    last = parts[-1]
    country: str | None = None
    region: str | None = None

    # "San Francisco, CA" / "Toronto, ON": a two-letter state/province code after a city.
    if len(parts) >= 2 and len(last) == 2 and last.isupper():
        if last.lower() in us:
            country, region = "US", us[last.lower()]
        elif last.lower() in ca:
            country, region = "CA", ca[last.lower()]
        if country:
            return Location(city=parts[-2], region=region, country=country)

    country = normalize_country(last)
    if country:
        parts = parts[:-1]
    elif last.lower() in us and len(last) > 2:  # "Austin, Texas"
        country, region, parts = "US", us[last.lower()], parts[:-1]
    elif last.lower() in ca and len(last) > 2:  # "Montreal, Quebec"
        country, region, parts = "CA", ca[last.lower()], parts[:-1]
    elif last.lower() in au and len(last) > 2:
        country, region, parts = "AU", au[last.lower()], parts[:-1]

    if region is None and len(parts) >= 2:
        region = normalize_region(parts[-1], country)
        parts = parts[:-1]
    city = parts[0] if parts else None
    if city and country is None and len(parts) == 1 and city.lower() in us:
        return Location(region=us[city.lower()], country="US")
    return Location(city=city, region=region, country=country)


# ---------------------------------------------------------------------------
# Remote / hybrid / onsite
# ---------------------------------------------------------------------------

_HYBRID = re.compile(r"\bhybrid\b", re.IGNORECASE)
_REMOTE = re.compile(
    r"(?<!non[- ])(?<!not )(?<!no )\b(?:remote|work from home|wfh|telecommute|distributed)\b",
    re.IGNORECASE,
)
_ONSITE = re.compile(
    r"\b(?:on-?site|in[- ]office|in[- ]person|office[- ]based|non[- ]remote)\b", re.IGNORECASE
)

_WORKPLACE_VALUES = {
    "remote": RemoteType.REMOTE,
    "hybrid": RemoteType.HYBRID,
    "onsite": RemoteType.ONSITE,
    "on-site": RemoteType.ONSITE,
    "on_site": RemoteType.ONSITE,
    "office": RemoteType.ONSITE,
    "in_office": RemoteType.ONSITE,
}


def remote_type_from_value(value: str | None) -> RemoteType:
    """Map an explicit workplace-type field ("Remote", "OnSite", "hybrid") to RemoteType."""
    if not value:
        return RemoteType.UNKNOWN
    return _WORKPLACE_VALUES.get(value.strip().lower().replace(" ", ""), RemoteType.UNKNOWN)


def detect_remote_type(*texts: str | None) -> RemoteType:
    """Detect the work arrangement from short texts such as location and title.

    Do not pass full descriptions: phrases like "our remote-first culture" are too common.
    """
    text = " ".join(t for t in texts if t)
    if not text:
        return RemoteType.UNKNOWN
    if _HYBRID.search(text):
        return RemoteType.HYBRID
    if _REMOTE.search(text):
        return RemoteType.REMOTE
    if _ONSITE.search(text):
        return RemoteType.ONSITE
    return RemoteType.UNKNOWN


# ---------------------------------------------------------------------------
# Salary
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Salary:
    min: Decimal | None
    max: Decimal | None
    currency: str | None
    period: str | None  # "hour" | "day" | "week" | "month" | "year"


_CURRENCY_SYMBOLS = {
    "US$": "USD",
    "C$": "CAD",
    "CA$": "CAD",
    "A$": "AUD",
    "AU$": "AUD",
    "NZ$": "NZD",
    "S$": "SGD",
    "HK$": "HKD",
    "R$": "BRL",
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "₹": "INR",
    "₩": "KRW",
    "₪": "ILS",
    "zł": "PLN",
    "CHF": "CHF",
    "Fr.": "CHF",
}
_CURRENCY_CODES = (
    "USD|EUR|GBP|CAD|AUD|NZD|CHF|JPY|CNY|INR|SGD|HKD|SEK|NOK|DKK|PLN|CZK|HUF|RON|BRL|MXN|"
    "ZAR|ILS|AED|KRW|TRY|ARS|COP|CLP"
)
_SYMBOL_RE = "|".join(re.escape(s) for s in sorted(_CURRENCY_SYMBOLS, key=len, reverse=True))
_CUR = rf"(?:{_SYMBOL_RE}|\b(?:{_CURRENCY_CODES})\b)"
_NUM = r"\d{1,3}(?:[,.'’\u00a0 ]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d+)?"
_NOT_MILLIONS = r"(?!\s*(?:m\b|mm\b|million|billion|bn\b|b\b))"


def _amount(i: int) -> str:
    """Regex for one amount with optional currency before/after and optional "k" suffix."""
    return (
        rf"(?P<cur{i}a>{_CUR})?\s?(?P<num{i}>{_NUM})(?!\d|[.,]\d)\s?(?P<k{i}>[kK]\b)?"
        rf"{_NOT_MILLIONS}\s?(?P<cur{i}b>{_CUR})?"
    )


_PERIOD_WORDS = {
    "hour": "hour",
    "hr": "hour",
    "h": "hour",
    "hourly": "hour",
    "day": "day",
    "daily": "day",
    "week": "week",
    "wk": "week",
    "weekly": "week",
    "month": "month",
    "mo": "month",
    "monthly": "month",
    "year": "year",
    "yr": "year",
    "annum": "year",
    "annual": "year",
    "annually": "year",
    "yearly": "year",
    "annualized": "year",
}
_PERIOD = (
    r"(?:\s*(?:/|per|an|a|an\s+|each)\s*|\s+)"
    r"(?P<period>hour|hr|h|day|week|wk|month|mo|year|yr|annum)\b"
    r"|\s*(?P<period_adj>hourly|daily|weekly|monthly|annually|annual|yearly|annualized)\b"
)
_SALARY_RE = re.compile(
    _amount(1) + r"(?:\s*(?:-|–|—|to|bis|à)\s*" + _amount(2) + rf")?(?:{_PERIOD})?",
    re.IGNORECASE,
)


def _to_decimal(num: str, thousands: bool) -> Decimal | None:
    num = num.replace("\u00a0", " ").replace("’", "'")
    if re.fullmatch(r"\d{1,3}(?:[,.' ]\d{3})+", num):
        cleaned = re.sub(r"[,.' ]", "", num)  # every separator is a thousands separator
    elif re.fullmatch(r"\d{1,3}(?:[,.' ]\d{3})+[.,]\d{1,2}", num):
        cleaned = re.sub(r"[,.' ]", "", num[:-3] if num[-3] in ".," else num[:-2])
        cleaned += "." + re.split(r"[.,]", num)[-1]
    else:
        cleaned = num.replace(",", ".")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return value * 1000 if thousands else value


def _currency(token: str | None) -> str | None:
    if not token:
        return None
    return _CURRENCY_SYMBOLS.get(token) or _CURRENCY_SYMBOLS.get(token.upper()) or token.upper()


def infer_salary_period(amount: Decimal | None) -> str | None:
    """Guess the period from magnitude only when it is unambiguous."""
    if amount is None:
        return None
    if amount < 500:
        return "hour"
    if amount >= 20000:
        return "year"
    return None  # 500..20000 could be weekly or monthly: don't guess


def normalize_salary_period(value: str | None) -> str | None:
    """Map interval spellings ("1 YEAR", "per-year-salary", "Annually", "hourly") to a period."""
    if not value:
        return None
    for word in re.findall(r"[a-z]+", value.lower()):
        if word in _PERIOD_WORDS:
            return _PERIOD_WORDS[word]
    return None


def _match_to_salary(m: re.Match[str]) -> Salary | None:
    k1, k2 = bool(m["k1"]), bool(m["k2"])
    if m["num2"]:
        # A single "k" applies to both ends of the range: "120-150k", "120k-150".
        plain1, plain2 = _to_decimal(m["num1"], False), _to_decimal(m["num2"], False)
        if k2 and not k1 and plain1 is not None and plain1 < 1000:
            k1 = True
        if k1 and not k2 and plain2 is not None and plain2 < 1000:
            k2 = True
    lo = _to_decimal(m["num1"], k1)
    hi = _to_decimal(m["num2"], k2) if m["num2"] else None
    if lo is None or lo <= 0:
        return None
    if hi is not None and hi < lo:
        lo, hi = hi, lo
    currency = _currency(m["cur1a"] or m["cur1b"] or m["cur2a"] or m["cur2b"])
    period = _PERIOD_WORDS.get((m["period"] or m["period_adj"] or "").lower())
    if period is None:
        period = infer_salary_period(hi if hi is not None else lo)
    return Salary(min=lo, max=hi if hi is not None else lo, currency=currency, period=period)


def parse_salary(text: str | None) -> Salary | None:
    """Parse a string known to describe a salary, e.g. ``"$120k – $150k / year"``."""
    if not text:
        return None
    for m in _SALARY_RE.finditer(text):
        salary = _match_to_salary(m)
        if salary is not None:
            return salary
    return None


_SALARY_CONTEXT = re.compile(
    r"salary|\bpay\b|pay range|compensation|\bbase\b|\brange\b|\bwage|\bote\b|on-target|"
    r"earn|remuneration|gehalt|salaire|retribuci|stipendio",
    re.IGNORECASE,
)
_BENEFIT_CONTEXT = re.compile(
    r"stipend|allowance|reimburse|\bhra\b|\bhsa\b|\bfsa\b|coverage|budget|401|match|"
    r"bonus|referral|relocation|sign-?on|learning|wellness|fertility|equity|funding|raised|"
    r"revenue|spend|valuation",
    re.IGNORECASE,
)
_CONTEXT_CHARS = 120


def find_salary(text: str | None) -> Salary | None:
    """Find a salary inside longer text (e.g. a job description).

    Much stricter than :func:`parse_salary`. A match needs a currency marker, and either
    a salary keyword ("salary", "pay range", "compensation", ...) shortly before it, or
    both a range and a period ("$40 - $50 per hour"). Matches preceded by benefit or
    company-finance words ("stipend", "HRA", "funding", ...) are skipped, so
    "Fertility HRA (up to $10,000 per year)" or "$10M raised" are not salaries.
    """
    if not text:
        return None
    for m in _SALARY_RE.finditer(text):
        if not any(m[g] for g in ("cur1a", "cur1b", "cur2a", "cur2b")):
            continue
        context = text[max(0, m.start() - _CONTEXT_CHARS) : m.start()]
        # Only the current sentence/bullet counts as context.
        context = re.split(r"[.!?\n•]\s|\n", context)[-1]
        if _BENEFIT_CONTEXT.search(context):
            continue
        has_range = m["num2"] is not None
        has_period = bool(m["period"] or m["period_adj"])
        if not (_SALARY_CONTEXT.search(context) or (has_range and has_period)):
            continue
        salary = _match_to_salary(m)
        if salary is not None and salary.min is not None and salary.min >= 10:
            return salary
    return None


# ---------------------------------------------------------------------------
# Employment type
# ---------------------------------------------------------------------------

_EMPLOYMENT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"intern(ship)?|co-?op|praktikum|stage\b|werkstudent|working student"),
        "internship",
    ),
    (re.compile(r"apprentic|ausbildung"), "apprenticeship"),
    (re.compile(r"part[\s_-]?time|teilzeit"), "part_time"),
    (re.compile(r"contract|freelance|contractor|consultant"), "contract"),
    (
        re.compile(r"temp(orary)?\b|fixed[\s_-]?term|short[\s_-]?term|seasonal|befristet"),
        "temporary",
    ),
    (re.compile(r"full[\s_-]?time|permanent|regular|vollzeit|unbefristet|cdi\b"), "full_time"),
]


def normalize_employment_type(value: str | None) -> str | None:
    """Map free-text employment types to: full_time, part_time, contract, temporary,
    internship, apprenticeship. Unknown values return ``None``."""
    if not value:
        return None
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value).lower()  # "FullTime" -> "full time"
    for pattern, normalized in _EMPLOYMENT_PATTERNS:
        if pattern.search(text):
            return normalized
    return None


# ---------------------------------------------------------------------------
# Content hash
# ---------------------------------------------------------------------------

#: Fields whose change means the job itself changed. URLs, raw HTML and timestamps are
#: excluded on purpose: they change for cosmetic reasons (tracking params, markup).
HASH_FIELDS = (
    "title",
    "company",
    "description_text",
    "location_raw",
    "city",
    "region",
    "country",
    "remote_type",
    "employment_type",
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_period",
)


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, Enum):
        return str(value.value)
    raise TypeError(f"unhashable type {type(value)!r}")


def content_hash(fields: Mapping[str, Any], keys: tuple[str, ...] = HASH_FIELDS) -> str:
    """SHA-256 over ``keys`` of ``fields`` (missing keys count as ``None``)."""
    payload = {key: fields.get(key) for key in keys}
    data = json.dumps(payload, sort_keys=True, default=_json_default, ensure_ascii=False)
    return hashlib.sha256(data.encode()).hexdigest()
