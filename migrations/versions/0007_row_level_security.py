"""Row Level Security on every public table; nothing for Supabase's API roles

Supabase flags tables in ``public`` without RLS (``rls_disabled_in_public``): with the
Data API (PostgREST) on, the project's anon key could read them. JobIndex never uses the
Data API (the app connects through the pooler), so this locks it out at the database:

* RLS is enabled on every table in ``public`` (``alembic_version`` and
  ``pending_sources`` included), with explicit policies for our own roles only:
  ``owner_all`` for the role running migrations and the crawler (the table owner, which
  bypasses RLS anyway), ``web_read`` (SELECT) for ``jobindex_web`` on every table and
  ``web_insert`` on ``pending_sources``. ``jobindex_web``'s grants are unchanged
  (SELECT everywhere, INSERT on ``pending_sources``, docs/DEPLOY.md).
* ``anon`` and ``authenticated`` lose every privilege on tables, sequences and functions
  in ``public``, and the migration role's default privileges stop granting them anything
  on objects it creates later. They get no policies.

Roles that do not exist (local docker, a VPS) are skipped. A table added by a later
migration must enable RLS and add the same policies; ``tests/test_db_security.py`` and
``python -m jobindex.cli db-security`` (run by the migrate workflow) fail otherwise.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WEB_ROLE = "jobindex_web"
API_ROLES = ("anon", "authenticated")

_TABLES = """
    SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
"""


def upgrade() -> None:
    op.execute(
        f"""
        DO $$
        DECLARE
          t text;
          web boolean := EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{WEB_ROLE}');
        BEGIN
          FOR t IN {_TABLES} LOOP
            EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
            EXECUTE format('CREATE POLICY owner_all ON public.%I FOR ALL TO CURRENT_USER'
                           ' USING (true) WITH CHECK (true)', t);
            IF web THEN
              EXECUTE format('CREATE POLICY web_read ON public.%I FOR SELECT'
                             ' TO {WEB_ROLE} USING (true)', t);
            END IF;
          END LOOP;
          IF web THEN
            CREATE POLICY web_insert ON public.pending_sources FOR INSERT
              TO {WEB_ROLE} WITH CHECK (true);
          END IF;
        END $$;
        """
    )
    for role in API_ROLES:
        op.execute(
            f"""
            DO $$
            BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role};
                REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {role};
                REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM {role};
                ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM {role};
                ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM {role};
                ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM {role};
              END IF;
            END $$;
            """
        )


def downgrade() -> None:
    # Drops the policies and RLS; the revoked API-role privileges are not given back.
    op.execute(
        f"""
        DO $$
        DECLARE t text;
        BEGIN
          FOR t IN {_TABLES} LOOP
            EXECUTE format('DROP POLICY IF EXISTS owner_all ON public.%I', t);
            EXECUTE format('DROP POLICY IF EXISTS web_read ON public.%I', t);
            EXECUTE format('DROP POLICY IF EXISTS web_insert ON public.%I', t);
            EXECUTE format('ALTER TABLE public.%I DISABLE ROW LEVEL SECURITY', t);
          END LOOP;
        END $$;
        """
    )
