"""Full-text relevance (jobindex/api/search.py): title matches rank far above description
matches, and a one-word query ignores words buried deep in long descriptions."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from jobindex.api.schemas import StatusFilter
from jobindex.api.search import LEAD_CHARS, SearchParams, search
from jobindex.models import Job, RemoteType, Source

T0 = datetime(2026, 11, 10, tzinfo=UTC)
FILLER = "We offer a great team, flexible hours and a modern office. " * 20  # > LEAD_CHARS

JOBS = [  # title, company, description
    ("Cybersecurity Engineer", "Shieldly", "Protect our customers' infrastructure."),
    ("Security Analyst", "Huntress", "Huntress is a cybersecurity company. Triage alerts."),
    (
        "Staff Software Engineer (Ruby/Rails)",
        "Huntress",
        "Build our Rails platform. "
        + FILLER
        + "Our mission is cybersecurity for small businesses.",
    ),
    (
        "Director, Head of Core Banking Technology",
        "PayPal",
        "Lead core banking. " + FILLER + "Partner with cybersecurity and risk teams.",
    ),
    (
        "Senior Regulatory Affairs Specialist",
        "Function Health",
        "Regulatory filings. " + FILLER + "Awareness of cybersecurity regulations is a plus.",
    ),
    ("Head of Cybersecurity", "Bankly", "Own the security programme."),
]


async def seed(session: AsyncSession) -> None:
    session.add(Source(id=1, platform="greenhouse", board_identifier="a", base_url="https://a"))
    await session.flush()
    for i, (title, company, text) in enumerate(JOBS):
        session.add(
            Job(
                source_id=1,
                external_id=str(i),
                title=title,
                company=company,
                description_text=text,
                remote_type=RemoteType.REMOTE,
                first_seen_at=T0,
                last_seen_at=T0,
                content_hash=str(i),
            )
        )
    await session.commit()


async def titles(session: AsyncSession, q: str) -> list[str]:
    found = await search(
        session, SearchParams(q=q, remote_type=RemoteType.REMOTE, status=StatusFilter.OPEN)
    )
    return [job.title for job in found.items]


def test_filler_is_longer_than_the_lead() -> None:
    assert len(FILLER) > LEAD_CHARS


async def test_one_word_query_title_first_buried_mentions_dropped(session: AsyncSession) -> None:
    await seed(session)
    result = await titles(session, "Cybersecurity")
    # title matches first, then a description that is about it from the first sentence
    assert set(result[:2]) == {"Cybersecurity Engineer", "Head of Cybersecurity"}
    assert result[2:] == ["Security Analyst"]
    # Ruby/Rails, banking director, regulatory affairs mention it only deep in the text
    buried = {
        "Staff Software Engineer (Ruby/Rails)",
        "Director, Head of Core Banking Technology",
        "Senior Regulatory Affairs Specialist",
    }
    assert not buried & set(result)


async def test_case_and_stemming(session: AsyncSession) -> None:
    await seed(session)
    assert await titles(session, "cybersecurity") == await titles(session, "CYBERSECURITY")
    assert "Security Analyst" in await titles(session, "security")


async def test_multi_word_queries_search_the_whole_text(session: AsyncSession) -> None:
    await seed(session)
    result = await titles(session, "cybersecurity banking")
    # both words, anywhere in the text ("Bankly" stems to "bank" too)
    assert set(result) == {"Director, Head of Core Banking Technology", "Head of Cybersecurity"}
    phrase = await titles(session, '"cybersecurity regulations"')
    assert phrase == ["Senior Regulatory Affairs Specialist"]
    assert "Staff Software Engineer (Ruby/Rails)" in await titles(session, "rails")


async def test_title_beats_description_for_multi_word(session: AsyncSession) -> None:
    await seed(session)
    result = await titles(session, "cybersecurity OR rails")
    assert result[0] in {
        "Cybersecurity Engineer",
        "Head of Cybersecurity",
        "Staff Software Engineer (Ruby/Rails)",
    }
    assert result.index("Senior Regulatory Affairs Specialist") > result.index(
        "Cybersecurity Engineer"
    )
