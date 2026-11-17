"""Local currency of a country, for salaries published without a currency.

Shown as "inferred from the location" on the pages; the stored ``salary_currency`` and the
JSON API stay as the source published them (``null``), so nothing is invented in the data.
"""

EURO = ["AT", "BE", "HR", "CY", "EE", "FI", "FR", "DE", "GR", "IE"]  # eurozone
EURO += ["IT", "LV", "LT", "LU", "MT", "NL", "PT", "SK", "SI", "ES"]

#: ISO 3166-1 alpha-2 -> ISO 4217 for the countries jobs are indexed in.
COUNTRY_CURRENCY: dict[str, str] = {
    **dict.fromkeys(EURO, "EUR"),
    "US": "USD", "CA": "CAD", "MX": "MXN", "BR": "BRL", "AR": "ARS", "CL": "CLP",
    "CO": "COP", "PE": "PEN", "GB": "GBP", "CH": "CHF", "NO": "NOK", "SE": "SEK",
    "DK": "DKK", "IS": "ISK", "PL": "PLN", "CZ": "CZK", "HU": "HUF", "RO": "RON",
    "BG": "BGN", "RS": "RSD", "UA": "UAH", "TR": "TRY", "IL": "ILS", "AE": "AED",
    "SA": "SAR", "QA": "QAR", "EG": "EGP", "MA": "MAD", "NG": "NGN", "KE": "KES",
    "ZA": "ZAR", "IN": "INR", "PK": "PKR", "BD": "BDT", "LK": "LKR", "SG": "SGD",
    "MY": "MYR", "ID": "IDR", "TH": "THB", "VN": "VND", "PH": "PHP", "HK": "HKD",
    "TW": "TWD", "CN": "CNY", "JP": "JPY", "KR": "KRW", "AU": "AUD", "NZ": "NZD",
}  # fmt: skip


def infer_currency(country: str | None) -> str | None:
    """The local currency of ``country`` (ISO alpha-2), or ``None`` if unknown."""
    return COUNTRY_CURRENCY.get((country or "").upper())
