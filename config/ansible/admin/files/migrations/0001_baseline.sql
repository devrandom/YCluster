-- Baseline schema for the admin database.
--
-- This migration captures the schema that existed before the migration
-- framework was introduced. Every statement is idempotent so it can be applied
-- safely to both fresh databases and databases provisioned by the old
-- provision-admin script.

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

-- Additive migration for deployments created before idp_project existed.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS idp_project
    TEXT NOT NULL DEFAULT '';

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
