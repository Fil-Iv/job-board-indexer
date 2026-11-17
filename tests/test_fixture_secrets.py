"""Saved real responses (tests/fixtures) must not carry credentials. The repository is
public and GitHub secret scanning flags them, even when they are third-party keys that
the page shows to every visitor (Google Maps browser keys, presigned S3 links).

When a new fixture fails this test, replace the value with ``REDACTED`` (connector tests
never need it)."""

import re

import pytest

from jobindex.coverage import ROOT

FIXTURES = ROOT / "tests" / "fixtures"
SECRETS = {
    "Google API key": re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    "AWS access key id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "AWS presigned signature": re.compile(r"X-Amz-Signature=[0-9a-f]{16,}"),
    "AWS session token": re.compile(r"X-Amz-Security-Token=(?!REDACTED)[^&\"\s<]{16,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\bgh[pousr]_[0-9A-Za-z]{36,}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}\b"),
    "Stripe live key": re.compile(r"\b[sr]k_live_[0-9A-Za-z]{16,}\b"),
}
FILES = sorted(p for p in FIXTURES.rglob("*") if p.is_file())


def test_fixtures_exist() -> None:
    assert len(FILES) > 100


@pytest.mark.parametrize("kind", sorted(SECRETS))
def test_no_credentials_in_fixtures(kind: str) -> None:
    pattern = SECRETS[kind]
    found = [
        str(p.relative_to(ROOT))
        for p in FILES
        if pattern.search(p.read_text(encoding="utf-8", errors="replace"))
    ]
    assert not found, f"{kind} in {found}: replace the value with REDACTED"
