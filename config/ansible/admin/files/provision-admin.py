#!/usr/bin/env python3
"""Provision the admin database, role, and schema migrations.

Runs as the postgres OS user (peer authentication) so it can create the
role and database. All statements go through psycopg2 with parameter
binding; the only interpolated values are the fixed ROLE/DATABASE names
this script owns.
"""
import hashlib
import logging
import os
import re

PASSWORD_KEY = "/cluster/config/ycluster/db-password"
ROLE = "ycluster"
DATABASE = "ycluster"

# Versioned SQL migrations are copied next to the provisioning script by
# admin-projects.yml. The env override keeps local runs and tests explicit.
MIGRATIONS_DIR = os.environ.get(
    "ADMIN_MIGRATIONS_DIR", "/usr/local/lib/ycluster/migrations")
MIGRATIONS_TABLE = "schema_migrations"
MIGRATION_FILENAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
# Session-level advisory lock so two provisioning runs can't race. The value is
# arbitrary but stable; it must not change after migrations have shipped.
MIGRATION_LOCK_KEY = 0x59434C5553544552

LOG = logging.getLogger(__name__)


def get_password():
    """Read the DB password from the environment.

    The playbook fetches it from etcd as root (the postgres OS user cannot
    read the etcd client certs) and passes it via ADMIN_DB_PASSWORD.
    """
    password = os.environ.get("ADMIN_DB_PASSWORD")
    if not password:
        raise Exception("ADMIN_DB_PASSWORD environment variable "
                        "is required (the playbook sets it from etcd)")
    return password


def connect(db):
    """Peer-authenticated connection as the postgres OS user."""
    # Lazy import: other admin test modules stub psycopg2 in sys.modules,
    # so importing it at module scope would break test isolation.
    import psycopg2
    return psycopg2.connect(dbname=db, user="postgres")


def database_exists(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s",
                    (DATABASE,))
        return cur.fetchone() is not None


def discover_migrations(directory):
    """Return SQL migrations in version order.

    Files must be named ``<version>_<slug>.sql``. Versions are unique and
    ordered lexicographically, so zero-pad them (``0001``, ``0002``, ...).
    """
    if not os.path.isdir(directory):
        raise RuntimeError(f"migration directory not found: {directory}")
    migrations = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".sql"):
            continue
        match = MIGRATION_FILENAME_RE.match(name)
        if not match:
            raise RuntimeError(f"invalid migration filename: {name}")
        path = os.path.join(directory, name)
        with open(path, "rb") as handle:
            body = handle.read()
        migrations.append({
            "version": match.group(1),
            "name": name,
            "path": path,
            "checksum": hashlib.sha256(body).hexdigest(),
            "sql": body.decode("utf-8"),
        })
    versions = [migration["version"] for migration in migrations]
    if len(versions) != len(set(versions)):
        raise RuntimeError("duplicate migration version")
    return migrations


def ensure_migrations_table(cur):
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {MIGRATIONS_TABLE} (
            version TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            applied_by TEXT NOT NULL DEFAULT ''
        )
    """)


def applied_migrations(cur):
    cur.execute(f"SELECT version, checksum FROM {MIGRATIONS_TABLE}")
    return dict(cur.fetchall())


def apply_migrations(conn, directory=None):
    """Apply pending migrations, one transaction per migration.

    A session-level advisory lock serializes concurrent provisioning runs.
    Already-applied migrations are checksummed so an edited file fails loudly
    instead of silently diverging from the database.
    """
    migrations = discover_migrations(directory or MIGRATIONS_DIR)
    known = {migration["version"] for migration in migrations}

    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
    try:
        with conn:
            with conn.cursor() as cur:
                ensure_migrations_table(cur)
        with conn:
            with conn.cursor() as cur:
                applied = applied_migrations(cur)

        unknown = sorted(set(applied) - known)
        if unknown:
            raise RuntimeError(
                "database has migrations not present on disk: "
                + ", ".join(unknown))

        versions = [migration["version"] for migration in migrations]
        applied_prefix = [version for version in versions
                          if version in applied]
        if applied_prefix != versions[:len(applied_prefix)]:
            raise RuntimeError(
                "applied migrations are not a prefix of the migration files")

        for migration in migrations:
            version = migration["version"]
            if version in applied:
                if applied[version] != migration["checksum"]:
                    raise RuntimeError(
                        f"migration {migration['name']} changed after it "
                        "was applied")
                continue
            with conn:
                with conn.cursor() as cur:
                    cur.execute(migration["sql"])
                    cur.execute(
                        f"""INSERT INTO {MIGRATIONS_TABLE}
                            (version, name, checksum, applied_by)
                            VALUES (%s, %s, %s, current_user)""",
                        (version, migration["name"], migration["checksum"]),
                    )
    finally:
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)",
                                (MIGRATION_LOCK_KEY,))
        except Exception:
            LOG.exception("failed to release admin migration lock")


# ROLE and DATABASE are fixed constants owned by this script, so they are
# interpolated here once; the password (the only runtime value) is always a
# bound %s parameter.
CREATE_ROLE_SQL = f"CREATE USER {ROLE} WITH PASSWORD %s"
ALTER_ROLE_SQL = f"ALTER USER {ROLE} WITH PASSWORD %s"
# postgres (superuser) owns the database and schema so the app role cannot
# drop its own audit machinery; the role gets only DML via GRANTs below.
CREATE_DATABASE_SQL = f"CREATE DATABASE {DATABASE}"
GRANT_CONNECT_SQL = f"GRANT CONNECT ON DATABASE {DATABASE} TO {ROLE}"
GRANT_SCHEMA_SQL = f"GRANT USAGE ON SCHEMA public TO {ROLE}"
GRANT_TABLES_SQL = \
    f"GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO {ROLE}"
GRANT_SEQUENCES_SQL = \
    f"GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO {ROLE}"
REVOKE_AUDIT_SQL = \
    f"REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM {ROLE}"
GRANT_AUDIT_SQL = f"GRANT INSERT, SELECT ON audit_events TO {ROLE}"
# Migration bookkeeping is operator-only; the app role must not rewrite it.
REVOKE_MIGRATIONS_SQL = \
    f"REVOKE ALL PRIVILEGES ON {MIGRATIONS_TABLE} FROM {ROLE}"


def create_role(cur, password):
    """Idempotently create the app role; always sync the password."""
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (ROLE,))
    if cur.fetchone() is None:
        cur.execute(CREATE_ROLE_SQL, (password,))
    cur.execute(ALTER_ROLE_SQL, (password,))


def create_database(cur):
    cur.execute(CREATE_DATABASE_SQL)


def apply_grants(cur):
    cur.execute(GRANT_CONNECT_SQL)
    cur.execute(GRANT_SCHEMA_SQL)
    cur.execute(GRANT_TABLES_SQL)
    cur.execute(GRANT_SEQUENCES_SQL)
    # Least privilege on the audit log: the app role may append and read
    # but never UPDATE, DELETE, or TRUNCATE audit history.
    cur.execute(REVOKE_AUDIT_SQL)
    cur.execute(GRANT_AUDIT_SQL)
    cur.execute(REVOKE_MIGRATIONS_SQL)


def provision(connect, password, migrations_dir=None):
    """Idempotent provision; `connect` takes a dbname and returns a conn."""
    # CREATE DATABASE/USER cannot run inside a transaction block, so this
    # phase needs autocommit.
    conn = connect("postgres")
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            create_role(cur, password)
            if not database_exists(conn):
                create_database(cur)
    finally:
        conn.close()

    conn = connect(DATABASE)
    try:
        apply_migrations(conn, migrations_dir)
        with conn:
            with conn.cursor() as cur:
                apply_grants(cur)
    finally:
        conn.close()


if __name__ == "__main__":
    password = get_password()
    provision(connect, password)
    print("admin provisioning complete")
