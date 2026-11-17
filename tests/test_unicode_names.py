"""Non-ASCII company names (ä ö ü ß é ñ) survive every step: HTTP decoding, page
parsing, normalisation, storage, search and the /pulse grouping.

("F mal s GmbH" on fmals.de is not a dropped "ü": it is the company's registered name,
see its imprint, Amtsgericht Köln HRB 80331.)"""

from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from jobindex.api import pulse
from jobindex.api.search import SearchParams, search
from jobindex.connectors.base import RawJob, company_name_from_page
from jobindex.dedup.keys import company_key
from jobindex.models import Job, Source
from tests.fakes import mock_http

NAMES = ["Müller & Söhne GmbH", "Straßenbau Größe AG", "Café Niño España S.L.", "Ætna Øst"]
T0 = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.mark.parametrize("name", NAMES)
def test_page_parsing_keeps_non_ascii(name: str) -> None:
    page = f'<html><head><meta property="og:site_name" content="{name}"></head></html>'
    assert company_name_from_page(page, ("og:site_name",)) == name


def test_html_entities_are_decoded() -> None:
    page = "<title>Jobs bei M&uuml;ller &amp; S&#246;hne GmbH &#8211; Karriere</title>"
    import re

    pattern = re.compile(r"Jobs bei (?P<name>.+?) – ")
    assert company_name_from_page(page, (pattern,)) == "Müller & Söhne GmbH"


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        ("Müller & Söhne – Straße, Café, Niño".encode(), "text/html; charset=UTF-8"),
        ("Müller & Söhne – Straße, Café, Niño".encode(), "text/html"),  # no charset
        ("Müller & Söhne, Straße, Café, Niño".encode("latin-1"), "text/html; charset=ISO-8859-1"),
    ],
)
async def test_http_decoding(body: bytes, content_type: str) -> None:
    http, _ = mock_http(
        {"example.org/": httpx.Response(200, content=body, headers={"Content-Type": content_type})}
    )
    async with http:
        text = (await http.get("https://example.org/")).text
    for word in ("Müller", "Söhne", "Straße", "Café", "Niño"):
        assert word in text


def test_normalisation_and_keys() -> None:
    raw = RawJob(external_id="1", title="Bäcker (m/w/d)", company=None)
    assert raw.normalized(company_fallback="Müller & Söhne GmbH").company == "Müller & Söhne GmbH"
    assert company_key("Müller & Söhne GmbH") == company_key("MULLER AND SOHNE") == "mullerandsohne"
    assert company_key("Café Niño España S.L.") == company_key("Cafe Nino Espana SL")


async def test_stored_searched_and_grouped(session: AsyncSession) -> None:
    session.add(Source(id=1, platform="greenhouse", board_identifier="a", base_url="https://a"))
    await session.flush()
    for i, name in enumerate([*NAMES, "MÜLLER & SÖHNE GMBH"]):
        session.add(
            Job(
                source_id=1,
                external_id=str(i),
                title="Bäcker",
                company=name,
                city="Köln",
                country="DE",
                first_seen_at=T0,
                last_seen_at=T0,
                content_hash=str(i),
            )
        )
    await session.commit()

    found = await search(session, SearchParams(company="müller"))
    assert {o.company for o in found.items} == {"Müller & Söhne GmbH", "MÜLLER & SÖHNE GMBH"}
    assert (await search(session, SearchParams(company="Straßenbau"))).total == 1
    assert (await search(session, SearchParams(company="Niño"))).total == 1

    companies = await pulse._top_companies(session, cap=0, open_openings=5)
    labels = {b.label: b.count for b in companies}
    # the two Müller spellings are one company; ties show the alphabetically first spelling
    assert labels == {
        "MÜLLER & SÖHNE GMBH": 2,
        "Café Niño España S.L.": 1,
        "Straßenbau Größe AG": 1,
        "Ætna Øst": 1,
    }
