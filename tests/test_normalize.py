from datetime import UTC, datetime
from decimal import Decimal

import pytest

from jobindex.connectors.base import RawJob
from jobindex.geo import country_of_city
from jobindex.models.enums import RemoteType
from jobindex.normalize import (
    Location,
    clean_company,
    clean_line,
    content_hash,
    detect_remote_type,
    find_salary,
    html_to_text,
    normalize_country,
    normalize_employment_type,
    normalize_salary_period,
    parse_datetime,
    parse_location,
    parse_salary,
    plausible_salary,
    split_url,
)


def test_html_to_text() -> None:
    html = (
        "<div><h2>About</h2><p>We   build\tthings.</p>"
        "<script>x()</script><ul><li>Python</li><li>SQL &amp; more</li></ul></div>"
    )
    assert html_to_text(html) == "About\nWe build things.\nPython\nSQL & more"
    assert html_to_text("<p>Build <b>great</b> <a href='#'>things</a>.</p><p>Next</p>") == (
        "Build great things.\nNext"
    )
    assert html_to_text("Line one<br>Line two<br/>") == "Line one\nLine two"
    assert html_to_text(None) is None
    assert html_to_text("   ") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("San Francisco, CA", Location("San Francisco", "CA", "US")),
        ("San Francisco, California, United States", Location("San Francisco", "CA", "US")),
        ("Austin, Texas", Location("Austin", "TX", "US")),
        ("Toronto, ON, Canada", Location("Toronto", "ON", "CA")),
        ("Montreal, Quebec", Location("Montreal", "QC", "CA")),
        # Region code + ISO country code: IN/CA/DE are countries here, not US states.
        ("Bengaluru, KA, IN", Location("Bengaluru", "KA", "IN")),
        ("Quebec City, QC, CA", Location("Quebec City", "QC", "CA")),
        ("Nuremberg, BY, DE", Location("Nuremberg", "BY", "DE")),
        ("Newark, DE, US", Location("Newark", "DE", "US")),
        ("Montreal, Québec, Canada", Location("Montreal", "QC", "CA")),
        ("Berlin, Germany", Location("Berlin", None, "DE")),
        ("London, UK", Location("London", None, "GB")),
        ("Bordeaux, Nouvelle-Aquitaine, France", Location("Bordeaux", "Nouvelle-Aquitaine", "FR")),
        ("Remote (U.S.)", Location(None, None, "US")),
        ("Remote - Canada", Location(None, None, "CA")),
        ("Remote", Location()),
        ("Europe", Location()),
        ("NYC", Location("New York", "NY", "US")),
        ("San Francisco Bay Area or Remote (U.S.)", Location("San Francisco", "CA", "US")),
        ("New York, NY (HQ); Remote (Canada)", Location("New York", "NY", "US")),
        ("San Francisco, CA • New York, NY • United States", Location("San Francisco", "CA", "US")),
        ("Hybrid - London, United Kingdom", Location("London", None, "GB")),
        ("Germany", Location(None, None, "DE")),
        ("US, CA, Santa Clara", Location("Santa Clara", "CA", "US")),
        ("DE, Munich", Location("Munich", None, "DE")),
        ("US, CA, Remote", Location(None, "CA", "US")),
        ("California", Location(None, "CA", "US")),
        # Bulgaria: bare city, Cyrillic, code, country-only remote (PARSER_VERSION 5)
        ("Sofia", Location("Sofia", None, "BG")),
        ("София", Location("Sofia", None, "BG")),
        ("Sofia, BG", Location("Sofia", None, "BG")),
        ("Plovdiv | Пловдив, BG", Location("Plovdiv", None, "BG")),
        ("Пловдив", Location("Plovdiv", None, "BG")),
        ("Bulgaria (Remote)", Location(None, None, "BG")),
        ("Remote - Bulgaria", Location(None, None, "BG")),
        # Bare big cities get their country only when the name is unambiguous
        ("Warsaw", Location("Warsaw", None, "PL")),
        ("Remote; Hamburg; Berlin", Location("Hamburg", None, "DE")),
        ("Valencia", Location("Valencia")),  # Venezuela and Spain
        ("Cambridge", Location("Cambridge")),  # small, GB and US
        # Region-only: no country (Job.area files them under the region, see test_area)
        ("Remote - EMEA", Location()),
        ("EU", Location()),
        # Country-first, dashes, repeated country, placeholders (PARSER_VERSION 6)
        ("India, Hyderabad", Location("Hyderabad", None, "IN")),
        ("Israel - Tel Aviv", Location("Tel Aviv", None, "IL")),
        ("US - United States", Location(None, None, "US")),
        ("Stratford-on-Avon - UK", Location("Stratford-on-Avon", None, "GB")),
        ("Republic of Ireland (Remote)", Location(None, None, "IE")),
        ("N/A", Location()),
        ("LOCATION", Location()),
        (None, Location()),
    ],
)
def test_parse_location(raw: str | None, expected: Location) -> None:
    assert parse_location(raw) == expected


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("United States", "US"),
        ("USA", "US"),
        ("us", "US"),
        ("Deutschland", "DE"),
        ("United Kingdom", "GB"),
        ("GBR", "GB"),
        ("European Union", None),
        ("", None),
    ],
)
def test_normalize_country(value: str, code: str | None) -> None:
    assert normalize_country(value) == code


@pytest.mark.parametrize(
    ("texts", "expected"),
    [
        (("Remote (US)",), RemoteType.REMOTE),
        (("New York, NY", "Senior Engineer (Hybrid)"), RemoteType.HYBRID),
        (("On-site - Berlin",), RemoteType.ONSITE),
        (("Non-remote role, Austin",), RemoteType.ONSITE),
        (("Work from home",), RemoteType.REMOTE),
        (("Berlin", "Engineer"), RemoteType.UNKNOWN),
        ((None,), RemoteType.UNKNOWN),
    ],
)
def test_detect_remote_type(texts: tuple[str | None, ...], expected: RemoteType) -> None:
    assert detect_remote_type(*texts) == expected


D = Decimal


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$120,000 - $150,000", (D(120000), D(150000), "USD", "year")),
        ("$120k–$150k per year", (D(120000), D(150000), "USD", "year")),
        ("$211.4K - $290.6K", (D("211400.0"), D("290600.0"), "USD", "year")),
        ("120-150k USD", (D(120000), D(150000), "USD", "year")),
        ("€50.000 – €60.000 brutto pro Jahr", (D(50000), D(60000), "EUR", "year")),
        ("EUR 4.500 per month", (D(4500), D(4500), "EUR", "month")),
        ("£40 per hour", (D(40), D(40), "GBP", "hour")),
        ("$25 - $30/hr", (D(25), D(30), "USD", "hour")),
        ("CHF 100'000 - 120'000", (D(100000), D(120000), "CHF", "year")),
        ("C$90,000 to C$110,000 annually", (D(90000), D(110000), "CAD", "year")),
        ("1,500.50 USD weekly", (D("1500.50"), D("1500.50"), "USD", "week")),
    ],
)
def test_parse_salary(text: str, expected: tuple[Decimal, Decimal, str, str]) -> None:
    salary = parse_salary(text)
    assert salary is not None
    assert (salary.min, salary.max, salary.currency, salary.period) == expected


def test_find_salary_in_description() -> None:
    spotify = (
        "The United States base range for this position is $87,069-$124,385 plus equity. "
        "The benefits available for this position include health insurance."
    )
    salary = find_salary(spotify)
    assert salary is not None
    assert (salary.min, salary.max, salary.currency) == (D(87069), D(124385), "USD")

    assert find_salary("Pay: $40 - $45 per hour, plus overtime") is not None


@pytest.mark.parametrize(
    "text",
    [
        "- Fertility HRA (up to $10,000 per year)",  # benefit, not salary
        "We have raised $10M from top investors.",
        "Over $275B in annualized spend flows through Ramp.",
        "401(k) plan with 4% match",
        "Learning budget of $1,000 per year",
        "We are 120 - 150 people.",  # no currency
    ],
)
def test_find_salary_rejects_non_salaries(text: str) -> None:
    assert find_salary(text) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Full-time", "full_time"),
        ("FullTime", "full_time"),
        ("Permanent", "full_time"),
        ("Part Time", "part_time"),
        ("Intern", "internship"),
        ("Contractor", "contract"),
        ("Short Term", "temporary"),
        ("Temporary", "temporary"),
        ("Werkstudent", "internship"),
        ("Something else", None),
        (None, None),
    ],
)
def test_normalize_employment_type(value: str | None, expected: str | None) -> None:
    assert normalize_employment_type(value) == expected


def test_normalize_salary_period() -> None:
    assert normalize_salary_period("1 YEAR") == "year"
    assert normalize_salary_period("per-hour-wage") == "hour"
    assert normalize_salary_period("Base Salary") is None


def test_content_hash_ignores_cosmetic_fields_and_key_order() -> None:
    a = {"title": "Engineer", "salary_min": D("100.00"), "source_url": "https://a?utm=1"}
    b = {"source_url": "https://a?utm=2", "salary_min": D("100"), "title": "Engineer"}
    assert content_hash(a) == content_hash(b)
    assert content_hash(a) != content_hash({**a, "title": "Senior Engineer"})


def test_parse_datetime() -> None:
    assert parse_datetime("2026-11-06T15:01:26.091Z") == datetime(
        2026, 11, 6, 15, 1, 26, 91000, tzinfo=UTC
    )
    assert parse_datetime(1792767640768) == datetime(2026, 10, 23, 15, 0, 40, 768000, tzinfo=UTC)
    assert parse_datetime("2026-11-03T11:27:16-05:00") == datetime(
        2026, 11, 3, 16, 27, 16, tzinfo=UTC
    )
    assert parse_datetime("2026-10-30 16:32:58 UTC") == datetime(
        2026, 10, 30, 16, 32, 58, tzinfo=UTC
    )
    assert parse_datetime("not a date") is None
    assert parse_datetime(None) is None


def test_split_url() -> None:
    assert split_url("www.Example.com/a%20b/c?x=1") == ("example.com", ["a b", "c"], {"x": ["1"]})


@pytest.mark.parametrize(
    ("city", "country", "expected"),
    [
        ("Sofia", None, "BG"),  # a connector's explicit city, no country
        ("Warsaw", None, "PL"),
        ("Valencia", None, None),  # ambiguous: left without a country
        ("London", "CA", "CA"),  # an explicit country always wins
    ],
)
def test_normalized_explicit_city_gets_its_country(
    city: str, country: str | None, expected: str | None
) -> None:
    job = RawJob(external_id="1", title="Engineer", city=city, country=country).normalized()
    assert (job.city, job.country) == (city, expected)


@pytest.mark.parametrize(
    ("name", "country"),
    [
        ("Sofia", "BG"),
        ("SOFIA", "BG"),
        ("Plovdiv", "BG"),
        ("Bucharest", "RO"),
        ("Kraków", "PL"),
        ("London", "GB"),  # 8.9 M vs 0.4 M in Canada
        ("Valencia", None),  # Venezuela 1.6 M vs Spain 0.8 M
        ("Barcelona", None),  # Spain 1.7 M vs Venezuela 0.8 M: not dominant enough
        ("Ulpia", None),  # an alternate name of Plovdiv, not its own name
        ("Atlantis", None),
    ],
)
def test_country_of_city(name: str, country: str | None) -> None:
    assert country_of_city(name) == country


@pytest.mark.parametrize(
    ("amounts", "currency", "period", "ok"),
    [
        ((Decimal(1), Decimal(2)), "USD", "year", False),  # Anthropic's "$1—$2 USD" placeholder
        ((Decimal(1), Decimal(1)), "GBP", "year", False),
        ((Decimal(120_000), Decimal(180_000)), "USD", "year", True),
        ((Decimal(999), None), "USD", "year", False),  # yearly < 1,000
        ((None, Decimal(6_000_000)), "USD", "year", False),  # yearly > 5,000,000
        ((Decimal(9_000_000), Decimal(12_000_000)), "JPY", "year", True),  # ≈ $61k–82k
        ((Decimal(1_200_000), None), "INR", "year", True),
        ((Decimal(25), Decimal(40)), "USD", "hour", True),
        ((Decimal("0.10"), None), "USD", "hour", False),
        ((Decimal(4_000), Decimal(6_000)), "EUR", "month", True),
        ((Decimal(500_000), None), "EUR", "month", False),  # 6 M a year
        ((Decimal(30), Decimal(45)), "USD", None, True),  # no period: could be hourly
        ((Decimal(0), Decimal(0)), "USD", None, False),
        ((Decimal(50_000_000), None), "XYZ", "year", True),  # unknown currency: no ceiling
        ((Decimal(5), None), "XYZ", "year", False),
    ],
)
def test_plausible_salary(
    amounts: tuple[Decimal | None, ...], currency: str, period: str | None, ok: bool
) -> None:
    assert plausible_salary(amounts, currency, period) is ok


def test_normalized_hides_implausible_salary() -> None:
    placeholder = RawJob(
        external_id="1",
        title="Legal Specialist",
        salary_min=Decimal(1),
        salary_max=Decimal(2),
        salary_currency="USD",
        salary_period="year",
    ).normalized()
    assert (placeholder.salary_min, placeholder.salary_max, placeholder.salary_currency) == (
        None,
        None,
        None,
    )
    assert placeholder.salary_period is None
    # no currency: judged in the location's currency (¥ figures are large)
    yen = RawJob(
        external_id="2",
        title="Engineer",
        country="JP",
        salary_min=Decimal(8_000_000),
        salary_period="year",
    ).normalized()
    assert yen.salary_min == Decimal(8_000_000)
    swapped = RawJob(
        external_id="3",
        title="Engineer",
        salary_min=Decimal(150_000),
        salary_max=Decimal(120_000),
        salary_currency="USD",
        salary_period="year",
    ).normalized()
    assert (swapped.salary_min, swapped.salary_max) == (Decimal(120_000), Decimal(150_000))


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Funds &amp; Index Analytics", "Funds & Index Analytics"),
        ("Account\u00a0Partner - Data Cloud", "Account Partner - Data Cloud"),
        ("Applied\u202fAI FDE", "Applied AI FDE"),
        ("Consultant  - Life Sciences ", "Consultant - Life Sciences"),
        ("Zero\u200bwidth", "Zerowidth"),
        ("R&D Engineer", "R&D Engineer"),
    ],
)
def test_clean_line(raw: str, clean: str) -> None:
    assert clean_line(raw) == clean


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Unknown", None),
        (" n/a ", None),
        ("-", None),
        ("", None),
        (None, None),
        ("Confidential", "Confidential"),  # says something: kept
        ("AT&amp;T", "AT&T"),
    ],
)
def test_clean_company(name: str | None, expected: str | None) -> None:
    assert clean_company(name) == expected


def test_normalized_cleans_title_and_placeholder_company() -> None:
    job = RawJob(external_id="1", title="Account\u00a0Partner", company="Unknown").normalized(
        company_fallback="Veeva Systems"
    )
    assert (job.title, job.company) == ("Account Partner", "Veeva Systems")
