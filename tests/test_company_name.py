"""``company_name_from_page`` (BaseConnector.company_name_sources)."""

import re

from jobindex.connectors.base import company_name_from_page

PAGE = (
    '<html><head><meta content="Oatly AB" property="og:site_name">'
    "<title>\n  Careers | Jobs at Focus Ireland &amp; Co\n</title></head></html>"
)


def test_og_site_name_and_title_patterns() -> None:
    assert company_name_from_page(PAGE, ("og:site_name",)) == "Oatly AB"
    jobs_at = re.compile(r"Jobs at (?P<name>.+)$")
    assert company_name_from_page(PAGE, (jobs_at,)) == "Focus Ireland & Co"
    # first source that yields a name wins; no match -> None
    assert company_name_from_page(PAGE, (re.compile(r"^X (?P<name>.+)"), jobs_at)) == (
        "Focus Ireland & Co"
    )
    assert company_name_from_page("<title>Careers</title>", (jobs_at, "og:site_name")) is None
