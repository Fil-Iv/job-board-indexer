"""Fails when normalize.py's behaviour changes without bumping PARSER_VERSION.

See tests/parser_corpus.py. Fix: bump ``jobindex.normalize.PARSER_VERSION`` (and add a
line to its history comment), then run ``make parser-fingerprint``.
"""

import json

from jobindex.normalize import PARSER_VERSION
from tests.parser_corpus import FINGERPRINT_FILE, fingerprint, load_corpus


def test_parser_version_matches_behaviour() -> None:
    recorded = json.loads(FINGERPRINT_FILE.read_text())
    current = fingerprint(load_corpus())
    if recorded["parser_version"] == PARSER_VERSION:
        assert current == recorded["digest"], (
            "normalize.py output changed but PARSER_VERSION was not bumped. Bump "
            "jobindex.normalize.PARSER_VERSION, then run `make parser-fingerprint`."
        )
    else:
        assert recorded["parser_version"] < PARSER_VERSION, "PARSER_VERSION must only increase"
        raise AssertionError(
            f"PARSER_VERSION is {PARSER_VERSION} but the recorded fingerprint is for "
            f"{recorded['parser_version']}: run `make parser-fingerprint`."
        )
