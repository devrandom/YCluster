#!/usr/bin/env python3
"""Provision the admin database, role, schema, and audit triggers.

Runs as the postgres OS user (peer authentication) so it can create the
role and database. All statements go through psycopg2 with parameter
binding; the only interpolated values are the fixed ROLE/DATABASE names
this script owns.
"""
import os
import subprocess
import sys

PASSWORD_KEY = "/cluster/config/ycluster/db-password"
ROLE = "ycluster"
DATABASE = "ycluster"

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    -- The Authentik IdP project this user project is nested under.
    -- Controls manager visibility: managers see user projects whose
    -- idp_project is in their manager_projects list. Empty = admin-only.
    idp_project TEXT NOT NULL DEFAULT '',
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS project_members (
    project_id TEXT NOT NULL REFERENCES projects(id),
    authentik_uid TEXT NOT NULL,
    email_at_assignment TEXT NOT NULL,
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, authentik_uid)
);
CREATE UNIQUE INDEX IF NOT EXISTS project_members_default_uid_idx
    ON project_members (authentik_uid) WHERE is_default;

CREATE TABLE IF NOT EXISTS quota_types (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    unit TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS project_quotas (
    project_id TEXT NOT NULL REFERENCES projects(id),
    quota_type TEXT NOT NULL REFERENCES quota_types(id),
    quota_id TEXT NOT NULL,
    value NUMERIC(18,4) NOT NULL CHECK (value >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, quota_type, quota_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_uid TEXT NOT NULL,
    actor_email TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    table_name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    before_data JSONB,
    after_data JSONB,
    request_id TEXT NOT NULL DEFAULT '',
    import_batch_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS audit_events_ts_idx ON audit_events (ts);
CREATE INDEX IF NOT EXISTS audit_events_entity_idx
    ON audit_events (table_name, entity_id);

CREATE OR REPLACE FUNCTION ycluster_audit_function() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    old_data JSONB;
    new_data JSONB;
    entity_id TEXT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        old_data := to_jsonb(OLD);
        new_data := NULL;
    ELSE
        old_data := CASE WHEN TG_OP = 'UPDATE' THEN to_jsonb(OLD) END;
        new_data := to_jsonb(NEW);
    END IF;
    entity_id := COALESCE(
        new_data ->> 'id',
        old_data ->> 'id',
        COALESCE(new_data ->> 'project_id', old_data ->> 'project_id') ||
        '/' || COALESCE(new_data ->> 'authentik_uid',
                        old_data ->> 'authentik_uid',
                        new_data ->> 'quota_id', old_data ->> 'quota_id'),
        new_data ->> 'quota_id', old_data ->> 'quota_id');
    IF entity_id IS NULL OR entity_id = '/' THEN
        entity_id := COALESCE(new_data ->> 'quota_id',
                              old_data ->> 'quota_id', '');
    END IF;
    INSERT INTO audit_events (
        actor_uid, actor_email, action, table_name, entity_type,
        entity_id, before_data, after_data, request_id, import_batch_id
    ) VALUES (
        COALESCE(NULLIF(current_setting('ycluster.actor_uid', true), ''),
                 'unknown'),
        COALESCE(current_setting('ycluster.actor_email', true), ''),
        lower(TG_OP),
        TG_TABLE_NAME,
        TG_TABLE_NAME,
        entity_id,
        old_data,
        new_data,
        COALESCE(current_setting('ycluster.request_id', true), ''),
        COALESCE(current_setting('ycluster.import_batch_id', true), '')
    );
    RETURN COALESCE(NEW, OLD);
END;
$$;

DO $$
DECLARE table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['projects', 'project_members',
                                      'project_quotas'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS ycluster_audit ON %I', table_name);
        EXECUTE format(
            'CREATE TRIGGER ycluster_audit AFTER INSERT OR UPDATE OR DELETE
             ON %I FOR EACH ROW EXECUTE FUNCTION ycluster_audit_function()',
            table_name);
    END LOOP;
END $$;

CREATE OR REPLACE FUNCTION ycluster_audit_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only';
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS audit_events_immutable ON audit_events;
CREATE TRIGGER audit_events_immutable
BEFORE UPDATE OR DELETE ON audit_events
FOR EACH ROW EXECUTE FUNCTION ycluster_audit_immutable();

-- TRUNCATE bypasses row-level triggers, so guard it with a statement-level
-- trigger as well; without this the app role could wipe the audit trail.
CREATE OR REPLACE FUNCTION ycluster_audit_no_truncate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only (TRUNCATE blocked)';
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS audit_events_no_truncate ON audit_events;
CREATE TRIGGER audit_events_no_truncate
BEFORE TRUNCATE ON audit_events
FOR EACH STATEMENT EXECUTE FUNCTION ycluster_audit_no_truncate();

INSERT INTO quota_types (id, display_name, unit)
VALUES ('gpu-hours', 'GPU hours', 'gpu-hours')
ON CONFLICT (id) DO UPDATE
SET display_name = EXCLUDED.display_name, unit = EXCLUDED.unit;
"""


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
    # so importing at module scope would break test isolation.
    import psycopg2
    return psycopg2.connect(dbname=db, user="postgres")


def database_exists(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s",
                    (DATABASE,))
        return cur.fetchone() is not None


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


def create_role(cur, password):
    """Idempotently create the app role; always sync the password."""
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (ROLE,))
    if cur.fetchone() is None:
        cur.execute(CREATE_ROLE_SQL, (password,))
    cur.execute(ALTER_ROLE_SQL, (password,))


def create_database(cur):
    cur.execute(CREATE_DATABASE_SQL)


def apply_schema(cur):
    cur.execute(SCHEMA)
    # Additive migration for deployments created before idp_project existed.
    cur.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS idp_project "
                "TEXT NOT NULL DEFAULT ''")
    cur.execute(GRANT_CONNECT_SQL)
    cur.execute(GRANT_SCHEMA_SQL)
    cur.execute(GRANT_TABLES_SQL)
    cur.execute(GRANT_SEQUENCES_SQL)
    # Least privilege on the audit log: the app role may append and read
    # but never UPDATE, DELETE, or TRUNCATE audit history.
    cur.execute(REVOKE_AUDIT_SQL)
    cur.execute(GRANT_AUDIT_SQL)


def provision(connect, password):
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
        with conn:
            with conn.cursor() as cur:
                apply_schema(cur)
    finally:
        conn.close()


if __name__ == "__main__":
    password = get_password()
    provision(connect, password)
    print("admin provisioning complete")
