from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Computed,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from jobindex.models.base import Base, utcnow
from jobindex.models.enums import JobStatus, RemoteType, enum_values

# Weighted full-text vector maintained by Postgres itself (generated column):
# title > company > description.
SEARCH_VECTOR_SQL = (
    "setweight(to_tsvector('english'::regconfig, coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('english'::regconfig, coalesce(company, '')), 'B') || "
    "setweight(to_tsvector('english'::regconfig, coalesce(description_text, '')), 'C')"
)


# Seniority level derived from the title by Postgres itself (generated column, so every
# row is classified on insert / title change and it can be indexed). Word-boundary
# regexes (\m \M), case-insensitive, several languages. The first matching level wins:
# internship > management > lead > senior > junior > mid; nothing -> 'unknown'.
# Keep in sync with SENIORITY_LEVELS (jobindex/api/schemas.py: Seniority).
_INTERNSHIP = (
    r"intern|interns|internship|internships|trainee|trainees|traineeship|graduate|graduates|"
    r"apprentice|apprenticeship|praktikant|praktikantin|praktikum|pflichtpraktikum|"
    r"werkstudent|werkstudentin|working student|ausbildung|azubi|stagiaire|stage|stagiair|"
    r"alternance|alternant|alternante|becario|becaria|pr[aá]cticas|tirocinio|tirocinante|"
    r"estagi[aá]rio|estagi[aá]ria|est[aá]gio"
)
_MANAGEMENT = (
    r"manager|managers|head|director|directors|vp|svp|evp|vice president|chief|cto|cfo|ceo|"
    r"coo|cio|ciso|cpo|leiter|leiterin|teamleiter|teamleiterin|abteilungsleiter|"
    r"abteilungsleiterin|gesch[aä]ftsf[uü]hrer|gesch[aä]ftsf[uü]hrerin|directeur|directrice|"
    r"responsable|gerente|jefe|direttore|direktor|direktorin"
)
_LEAD = r"staff|principal|lead|leads"
_SENIOR = (
    r"senior|sr|snr|s[eé]nior|iii|iv|expert|experienced|erfahren|erfahrene|erfahrener|"
    r"confirm[eé]|confirm[eé]e"
)
_JUNIOR = (
    r"junior|jr|jnr|associate|entry|einsteiger|berufseinsteiger|berufseinsteigerin|"
    r"d[eé]butant|d[eé]butante"
)
_MID = r"mid(?![- ]?market)|midlevel|intermediate|medior|ii|regular"
SENIORITY_SQL = (
    "CASE"
    + "".join(
        f" WHEN title ~* '\\m({words})\\M' THEN '{level}'"
        for level, words in (
            ("internship", _INTERNSHIP),
            ("management", _MANAGEMENT),
            ("lead", _LEAD),
            ("senior", _SENIOR),
            ("junior", _JUNIOR),
            ("mid", _MID),
        )
    )
    + " ELSE 'unknown' END"
)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id"),
        Index("ix_jobs_search_vector", "search_vector", postgresql_using="gin"),
        Index("ix_jobs_status_posted_at", "status", "posted_at"),
        Index("ix_jobs_seniority", "seniority"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sources.id", ondelete="CASCADE"), index=True
    )
    external_id: Mapped[str] = mapped_column(String(512))

    title: Mapped[str] = mapped_column(Text)
    company: Mapped[str | None] = mapped_column(Text)
    description_html: Mapped[str | None] = mapped_column(Text)
    description_text: Mapped[str | None] = mapped_column(Text)

    location_raw: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(String(255))
    region: Mapped[str | None] = mapped_column(String(255))
    country: Mapped[str | None] = mapped_column(String(255))
    remote_type: Mapped[RemoteType] = mapped_column(
        Enum(
            RemoteType,
            name="remote_type",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=enum_values,
        ),
        default=RemoteType.UNKNOWN,
    )
    employment_type: Mapped[str | None] = mapped_column(String(64))

    salary_min: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    salary_max: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    salary_currency: Mapped[str | None] = mapped_column(String(3))
    salary_period: Mapped[str | None] = mapped_column(String(16))

    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_url: Mapped[str | None] = mapped_column(Text)
    apply_url: Mapped[str | None] = mapped_column(Text)
    #: RawJob.expires_at: the posting is not open after this time.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: RawJob.list_version (e.g. sitemap lastmod): decides when to re-fetch the detail page.
    list_version: Mapped[str | None] = mapped_column(String(64))

    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    #: Hash of the posting exactly as the connector returned it (before normalisation);
    #: tells source changes apart from parser changes. NULL for rows from before 0002.
    raw_hash: Mapped[str | None] = mapped_column(String(64))
    #: normalize.PARSER_VERSION that produced the stored derived fields.
    parser_version: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[JobStatus] = mapped_column(
        Enum(
            JobStatus,
            name="job_status",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=enum_values,
        ),
        default=JobStatus.OPEN,
    )
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Set by the dedup step when the same job is posted on several sources.
    canonical_job_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("jobs.id", ondelete="SET NULL"), index=True
    )

    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed(SEARCH_VECTOR_SQL, persisted=True)
    )
    seniority: Mapped[str] = mapped_column(String(16), Computed(SENIORITY_SQL, persisted=True))
