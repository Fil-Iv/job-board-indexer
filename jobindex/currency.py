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


#: Rough USD value of one unit (order of magnitude, 2026), only to judge whether a
#: salary is plausible (normalize.plausible_salary) — never shown or used for conversion.
USD_PER_UNIT: dict[str, float] = {
    "USD": 1, "EUR": 1.1, "GBP": 1.3, "CHF": 1.15, "CAD": 0.73, "AUD": 0.66, "NZD": 0.6,
    "SGD": 0.75, "HKD": 0.13, "JPY": 0.0068, "CNY": 0.14, "KRW": 0.00073, "TWD": 0.031,
    "INR": 0.012, "PKR": 0.0036, "BDT": 0.0083, "LKR": 0.0033, "IDR": 0.000062,
    "VND": 0.00004, "THB": 0.029, "PHP": 0.017, "MYR": 0.22, "BRL": 0.18, "MXN": 0.055,
    "ARS": 0.001, "CLP": 0.00105, "COP": 0.00025, "PEN": 0.27, "PLN": 0.26, "CZK": 0.044,
    "HUF": 0.0028, "RON": 0.22, "BGN": 0.56, "RSD": 0.0094, "UAH": 0.024, "TRY": 0.029,
    "ILS": 0.27, "AED": 0.27, "SAR": 0.27, "QAR": 0.27, "EGP": 0.02, "MAD": 0.1,
    "NGN": 0.00065, "KES": 0.0077, "ZAR": 0.055, "SEK": 0.095, "NOK": 0.093, "DKK": 0.147,
    "ISK": 0.0072,
}  # fmt: skip
