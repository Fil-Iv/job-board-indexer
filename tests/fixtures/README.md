# Test fixtures

One directory per platform: `tests/fixtures/<platform>/`, holding **real** responses
(JSON or HTML) saved from the platform's public endpoint, e.g.

    tests/fixtures/greenhouse/board_jobs.json

Load them in tests with `tests.fakes.load_fixture()` / `load_json_fixture()`, and serve
them through `tests.fakes.mock_http()`.
When saving a fixture, trim it to a handful of postings and note the source URL and
capture date in the connector's test module docstring.
