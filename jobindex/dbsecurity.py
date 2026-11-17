"""Database access audit (``db-security`` CLI command, ``tests/test_db_security.py``).

The hosted demo runs on Supabase, whose Data API roles ``anon`` and ``authenticated``
must not reach JobIndex tables (migration 0007, docs/DEPLOY.md#database-access-lockdown).
``audit`` lists every violation of these rules in the ``public`` schema:

* every table has Row Level Security enabled;
* ``anon`` / ``authenticated`` hold no privilege on any table, sequence or function, and
  the current role's default privileges give them nothing on future objects;
* ``jobindex_web`` (if it exists) holds at most SELECT, plus INSERT on
  ``pending_sources``, and has a policy for each of those rights (else RLS hides rows).

Roles that do not exist are skipped, so the audit passes on a plain local database.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

API_ROLES = ("anon", "authenticated")
WEB_ROLE = "jobindex_web"
WEB_INSERT_TABLE = "pending_sources"

_RLS_DISABLED = """
SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relrowsecurity
ORDER BY 1
"""

# Explicit privileges (table, view, sequence, function) held by a role in `public`.
_GRANTS = """
SELECT c.relname AS object, r.rolname AS role, a.privilege_type AS privilege
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN LATERAL aclexplode(c.relacl) a
JOIN pg_roles r ON r.oid = a.grantee
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
  AND r.rolname = ANY(:roles)
UNION ALL
SELECT p.proname || '()', r.rolname, a.privilege_type
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
CROSS JOIN LATERAL aclexplode(p.proacl) a
JOIN pg_roles r ON r.oid = a.grantee
WHERE n.nspname = 'public' AND r.rolname = ANY(:roles)
ORDER BY 1, 2, 3
"""

# Default privileges of the current role (in `public` or global) that grant to `roles`.
_DEFAULT_GRANTS = """
SELECT d.defaclobjtype, r.rolname, a.privilege_type
FROM pg_default_acl d
CROSS JOIN LATERAL aclexplode(d.defaclacl) a
JOIN pg_roles r ON r.oid = a.grantee
WHERE d.defaclrole = (SELECT oid FROM pg_roles WHERE rolname = current_user)
  AND (d.defaclnamespace = 0
       OR d.defaclnamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'public'))
  AND r.rolname = ANY(:roles)
ORDER BY 1, 2, 3
"""

# Tables where `role` holds `privilege` but no policy lets it use it (polcmd: r = SELECT,
# a = INSERT, * = ALL; polroles {0} = PUBLIC).
_MISSING_POLICY = """
SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND c.relrowsecurity
  AND has_table_privilege(:role, c.oid, :privilege)
  AND NOT EXISTS (
    SELECT 1 FROM pg_policy p
    WHERE p.polrelid = c.oid AND p.polcmd IN (:cmd, '*') AND p.polpermissive
      AND (0 = ANY(p.polroles)
           OR (SELECT oid FROM pg_roles WHERE rolname = :role) = ANY(p.polroles)))
ORDER BY 1
"""

_OBJ_TYPES = {"r": "tables", "S": "sequences", "f": "functions", "T": "types", "n": "schemas"}


async def _role_exists(session: AsyncSession, role: str) -> bool:
    found = await session.scalar(
        text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
    )
    return found is not None


async def audit(session: AsyncSession) -> list[str]:
    """Return one line per problem; an empty list means the database is locked down."""
    problems = [
        f"table {name}: row level security is disabled"
        for name in (await session.scalars(text(_RLS_DISABLED))).all()
    ]
    roles = list(API_ROLES)
    for obj, role, privilege in await session.execute(text(_GRANTS), {"roles": roles}):
        problems.append(f"{obj}: {role} has {privilege}")
    for objtype, role, privilege in await session.execute(text(_DEFAULT_GRANTS), {"roles": roles}):
        kind = _OBJ_TYPES.get(objtype, objtype)
        problems.append(f"default privileges: new {kind} grant {privilege} to {role}")

    if await _role_exists(session, WEB_ROLE):
        for obj, _, privilege in await session.execute(text(_GRANTS), {"roles": [WEB_ROLE]}):
            allowed = (
                privilege == "SELECT"
                or (privilege == "INSERT" and obj == WEB_INSERT_TABLE)
                or (privilege == "USAGE" and obj == f"{WEB_INSERT_TABLE}_id_seq")
            )
            if not allowed:
                problems.append(f"{obj}: {WEB_ROLE} has {privilege} (only SELECT allowed)")
        for privilege, cmd in (("SELECT", "r"), ("INSERT", "a")):
            missing = await session.scalars(
                text(_MISSING_POLICY), {"role": WEB_ROLE, "privilege": privilege, "cmd": cmd}
            )
            problems += [
                f"table {name}: {WEB_ROLE} has {privilege} but no {privilege} policy"
                for name in missing.all()
            ]
    return problems
