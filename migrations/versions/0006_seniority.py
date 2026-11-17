"""jobs.seniority: level derived from the title (generated column) + index

Internship / junior / mid / senior / lead / management / unknown, computed by Postgres
from the title with word-boundary regexes in several languages (see
jobindex/models/job.py, SENIORITY_SQL). Adding a stored generated column rewrites the
table once.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy of jobindex.models.job.SENIORITY_SQL at this revision.
SENIORITY_SQL = (
    "CASE WHEN title ~* '\\m(intern|interns|internship|internships|trainee|trainees|traine"
    'eship|graduate|graduates|apprentice|apprenticeship|praktikant|praktikantin|praktikum'
    '|pflichtpraktikum|werkstudent|werkstudentin|working student|ausbildung|azubi|stagiai'
    're|stage|stagiair|alternance|alternant|alternante|becario|becaria|pr[aá]cticas|tiroc'
    "inio|tirocinante|estagi[aá]rio|estagi[aá]ria|est[aá]gio)\\M' THEN 'internship' WHEN "
    "title ~* '\\m(manager|managers|head|director|directors|vp|svp|evp|vice president|chie"
    'f|cto|cfo|ceo|coo|cio|ciso|cpo|leiter|leiterin|teamleiter|teamleiterin|abteilungslei'
    'ter|abteilungsleiterin|gesch[aä]ftsf[uü]hrer|gesch[aä]ftsf[uü]hrerin|directeur|direc'
    "trice|responsable|gerente|jefe|direttore|direktor|direktorin)\\M' THEN 'management' "
    "WHEN title ~* '\\m(staff|principal|lead|leads)\\M' THEN 'lead' WHEN title ~* '\\m(senio"
    'r|sr|snr|s[eé]nior|iii|iv|expert|experienced|erfahren|erfahrene|erfahrener|confirm[e'
    "é]|confirm[eé]e)\\M' THEN 'senior' WHEN title ~* '\\m(junior|jr|jnr|associate|entry|ei"
    "nsteiger|berufseinsteiger|berufseinsteigerin|d[eé]butant|d[eé]butante)\\M' THEN "
    "'junior' WHEN title ~* '\\m(mid(?![- "
    "]?market)|midlevel|intermediate|medior|ii|regular)\\M' THEN 'mid' ELSE 'unknown' END"
)


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "seniority",
            sa.String(length=16),
            sa.Computed(SENIORITY_SQL, persisted=True),
            nullable=False,
        ),
    )
    op.create_index("ix_jobs_seniority", "jobs", ["seniority"])


def downgrade() -> None:
    op.drop_index("ix_jobs_seniority", table_name="jobs")
    op.drop_column("jobs", "seniority")
