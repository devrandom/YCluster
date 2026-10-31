"""Domain layer for projects, project quotas, and their audit log.

The module is deliberately database-first: callers pass a DB-API cursor and
transaction context, which keeps request handlers testable without a live
PostgreSQL server. Schema ownership lives in `provision-admin.py` and its
versioned `migrations/` directory.
"""
import csv
import io
import json
import re
import uuid
from decimal import Decimal, InvalidOperation

CSV_COLUMNS = ("project-id", "quota-type", "quota-id", "value")
MAX_IMPORT_ROWS = 10000
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class ValidationError(ValueError):
    """A field-level validation error (message is safe to show an admin)."""


def clean_project_id(value):
    project_id = (value or "").strip().lower()
    if not PROJECT_ID_RE.match(project_id):
        raise ValidationError("project id must be 1-64 chars: a-z, 0-9, -")
    return project_id


def clean_text(value, label, max_length=200, required=False):
    value = (value or "").strip() if isinstance(value, str) else ""
    if required and not value:
        raise ValidationError(f"{label} is required")
    if len(value) > max_length:
        raise ValidationError(f"{label} is too long (max {max_length})")
    return value


def clean_quota_value(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError("value must be numeric")
    if not number.is_finite() or number < 0:
        raise ValidationError("value must be a finite non-negative number")
    # as_tuple().exponent is a str ("n"/"N"/"F") for NaN/Infinity; the
    # is_finite() check above guarantees an int here, but narrow it
    # explicitly so type checkers agree.
    exponent = number.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -4:
        raise ValidationError("value supports at most 4 decimal places")
    # NUMERIC(18,4): 14 integer digits + 4 fractional digits.
    if number.adjusted() + 1 > 14:
        raise ValidationError("value is too large (max 99999999999999.9999)")
    return number.normalize()


def _actor(identity):
    return {
        "uid": getattr(identity, "uid", None) or "internal",
        "email": getattr(identity, "email", None),
    }


def assert_project_in_scope(cur, project_id, allowed_idp_projects):
    """Raise ValidationError unless the project is under one of the given
    IdP projects. Managers pass their authorized set; admins pass None
    (unrestricted)."""
    if allowed_idp_projects is None:
        return
    cur.execute(
        "SELECT idp_project FROM projects WHERE id = %s",
        (clean_project_id(project_id),),
    )
    row = cur.fetchone()
    if not row or row[0] not in allowed_idp_projects:
        raise ValidationError("project outside your managed projects")

def set_actor(cur, identity):
    """Expose actor identity to database audit triggers for this transaction."""
    actor = _actor(identity)
    cur.execute("SET LOCAL ycluster.actor_uid = %s", (actor["uid"],))
    cur.execute("SET LOCAL ycluster.actor_email = %s",
                (actor["email"] or "",))


def _project_row(row):
    if not row:
        return None
    keys = ("id", "name", "description", "idp_project", "active",
            "created_at", "updated_at")
    return dict(zip(keys, row))


def _quota_row(row):
    if not row:
        return None
    keys = ("project_id", "quota_type", "quota_id", "value", "updated_at")
    data = dict(zip(keys, row))
    data["value"] = str(data["value"])
    return data


def get_project(cur, project_id):
    cur.execute(
        """SELECT id, name, description, idp_project, active, created_at,
                  updated_at
           FROM projects WHERE id = %s FOR UPDATE""",
        (clean_project_id(project_id),),
    )
    return _project_row(cur.fetchone())


def create_project(cur, identity, project_id, name, description="",
                   idp_project=""):
    project_id = clean_project_id(project_id)
    name = clean_text(name, "name", required=True)
    description = clean_text(description, "description", max_length=1000)
    idp_project = clean_text(idp_project, "IdP project", max_length=200)
    if get_project(cur, project_id):
        raise ValidationError("project already exists")
    cur.execute(
        """INSERT INTO projects (id, name, description, idp_project)
           VALUES (%s, %s, %s, %s)
           RETURNING id, name, description, idp_project, active, created_at,
                     updated_at""",
        (project_id, name, description, idp_project),
    )
    return _project_row(cur.fetchone())


def update_project(cur, identity, project_id, name=None, description=None,
                   active=None, idp_project=None, allowed_idp_projects=None):
    current = get_project(cur, project_id)
    if not current:
        raise ValidationError("no such project")
    assert_project_in_scope(cur, project_id, allowed_idp_projects)
    if idp_project is not None:
        idp_project = clean_text(idp_project, "IdP project", max_length=200)
        if allowed_idp_projects is not None \
                and idp_project not in allowed_idp_projects:
            raise ValidationError("cannot move project outside your managed projects")
    if name is None and description is None and active is None \
            and idp_project is None:
        return current
    fields = []
    values = []
    if name is not None:
        fields.append("name = %s")
        values.append(clean_text(name, "name", required=True))
    if description is not None:
        fields.append("description = %s")
        values.append(clean_text(description, "description", max_length=1000))
    if idp_project is not None:
        fields.append("idp_project = %s")
        values.append(idp_project)
    if active is not None:
        if not isinstance(active, bool):
            raise ValidationError("active must be boolean")
        fields.append("active = %s")
        values.append(active)
    fields.append("updated_at = now()")
    values.append(current["id"])
    cur.execute(
        "UPDATE projects SET " + ", ".join(fields) + " WHERE id = %s",
        values,
    )
    return get_project(cur, current["id"])


def list_projects(cur):
    cur.execute(
        """SELECT p.id, p.name, p.description, p.idp_project, p.active,
                  p.created_at, p.updated_at,
                  (SELECT COUNT(*) FROM project_members m
                   WHERE m.project_id = p.id)::int AS member_count,
                  (SELECT COUNT(*) FROM project_quotas q
                   WHERE q.project_id = p.id)::int AS quota_count
           FROM projects p
           ORDER BY p.id"""
    )
    return [
        dict(zip(("id", "name", "description", "idp_project", "active",
                  "created_at", "updated_at", "member_count",
                  "quota_count"), row))
        for row in cur.fetchall()
    ]


def list_projects_for_idp(cur, idp_projects):
    """Projects nested under the given IdP projects (manager scoping)."""
    if not idp_projects:
        return []
    cur.execute(
        """SELECT p.id, p.name, p.description, p.idp_project, p.active,
                  p.created_at, p.updated_at,
                  (SELECT COUNT(*) FROM project_members m
                   WHERE m.project_id = p.id)::int AS member_count,
                  (SELECT COUNT(*) FROM project_quotas q
                   WHERE q.project_id = p.id)::int AS quota_count
           FROM projects p
           WHERE p.idp_project = ANY(%s)
           ORDER BY p.id""",
        (sorted(idp_projects),),
    )
    return [
        dict(zip(("id", "name", "description", "idp_project", "active",
                  "created_at", "updated_at", "member_count",
                  "quota_count"), row))
        for row in cur.fetchall()
    ]


def list_members(cur, project_id):
    cur.execute(
        """SELECT project_id, authentik_uid, email_at_assignment, is_default,
                  created_at
           FROM project_members WHERE project_id = %s
           ORDER BY email_at_assignment, authentik_uid""",
        (clean_project_id(project_id),),
    )
    return [
        dict(zip(("project_id", "authentik_uid", "email_at_assignment",
                  "is_default", "created_at"), row))
        for row in cur.fetchall()
    ]


def list_quotas(cur, project_id):
    cur.execute(
        """SELECT project_id, quota_type, quota_id, value, updated_at
           FROM project_quotas WHERE project_id = %s
           ORDER BY quota_type, quota_id""",
        (clean_project_id(project_id),),
    )
    return [_quota_row(row) for row in cur.fetchall()]


def add_member(cur, identity, project_id, uid, email, is_default=False,
               allowed_idp_projects=None):
    if not get_project(cur, project_id):
        raise ValidationError("no such project")
    assert_project_in_scope(cur, project_id, allowed_idp_projects)
    uid = clean_text(uid, "user id", max_length=128, required=True)
    email = clean_text(email, "email", max_length=254)
    if not email:
        email = uid
    if is_default is None:
        is_default = False
    if not isinstance(is_default, bool):
        raise ValidationError("is_default must be boolean")
    cur.execute(
        """SELECT is_default FROM project_members
           WHERE project_id = %s AND authentik_uid = %s""",
        (project_id, uid),
    )
    row = cur.fetchone()
    was_default = bool(row[0]) if row else False
    # When promoting to default, clear the user's previous default first:
    # the partial unique index on (authentik_uid) WHERE is_default fires
    # immediately on INSERT/UPDATE. When NOT promoting, preserve an existing
    # default so re-adding a member never leaves them without one.
    if is_default and not was_default:
        cur.execute("UPDATE project_members SET is_default = FALSE"
                    " WHERE authentik_uid = %s", (uid,))
    final_default = is_default or was_default
    cur.execute(
        """INSERT INTO project_members
           (project_id, authentik_uid, email_at_assignment, is_default)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (project_id, authentik_uid)
           DO UPDATE SET email_at_assignment = EXCLUDED.email_at_assignment,
                         is_default = EXCLUDED.is_default""",
        (project_id, uid, email, final_default),
    )


def remove_member(cur, identity, project_id, uid, allowed_idp_projects=None):
    assert_project_in_scope(cur, project_id, allowed_idp_projects)
    uid = clean_text(uid, "user id", max_length=128, required=True)
    cur.execute(
        """DELETE FROM project_members
           WHERE project_id = %s AND authentik_uid = %s""",
        (clean_project_id(project_id), uid),
    )
    if cur.rowcount == 0:
        raise ValidationError("membership not found")


def set_default_member(cur, identity, project_id, uid, email=None,
                     allowed_idp_projects=None):
    assert_project_in_scope(cur, project_id, allowed_idp_projects)
    uid = clean_text(uid, "user id", max_length=128, required=True)
    project_id = clean_project_id(project_id)
    # Ensure membership exists, but never clobber the email captured at the
    # original assignment — the caller's identity is the operator, not the
    # member being promoted to default.
    cur.execute(
        """SELECT 1 FROM project_members
           WHERE project_id = %s AND authentik_uid = %s""",
        (project_id, uid),
    )
    if not cur.fetchone():
        add_member(cur, identity, project_id, uid,
                   clean_text(email or uid, "email"), False)
    cur.execute("UPDATE project_members SET is_default = FALSE"
                " WHERE authentik_uid = %s", (uid,))
    cur.execute(
        """UPDATE project_members SET is_default = TRUE
           WHERE project_id = %s AND authentik_uid = %s""",
        (project_id, uid),
    )
    if cur.rowcount == 0:
        raise ValidationError("membership not found")


def upsert_quota(cur, identity, project_id, quota_type, quota_id, value,
                 allowed_idp_projects=None):
    if not get_project(cur, project_id):
        raise ValidationError("no such project")
    assert_project_in_scope(cur, project_id, allowed_idp_projects)
    cur.execute("SELECT 1 FROM quota_types WHERE id = %s AND active",
                (clean_text(quota_type, "quota type", required=True),))
    if not cur.fetchone():
        raise ValidationError("no such quota type")
    quota_id = clean_text(quota_id, "quota id", max_length=128, required=True)
    value = clean_quota_value(value)
    cur.execute(
        """INSERT INTO project_quotas
           (project_id, quota_type, quota_id, value)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (project_id, quota_type, quota_id)
           DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        (project_id, quota_type, quota_id, value),
    )
    return _quota_row((project_id, quota_type, quota_id, value, None))


def delete_quota(cur, identity, project_id, quota_type, quota_id,
                  allowed_idp_projects=None):
    assert_project_in_scope(cur, project_id, allowed_idp_projects)
    cur.execute(
        """DELETE FROM project_quotas
           WHERE project_id = %s AND quota_type = %s AND quota_id = %s""",
        (clean_project_id(project_id),
         clean_text(quota_type, "quota type", required=True),
         clean_text(quota_id, "quota id", required=True)),
    )
    if cur.rowcount == 0:
        raise ValidationError("quota not found")


def parse_quota_csv(text):
    """Parse and validate all rows before any database mutation."""
    try:
        if isinstance(text, bytes):
            text = text.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text or "", newline=""))
        if not reader.fieldnames or tuple(reader.fieldnames) != CSV_COLUMNS:
            raise ValidationError("CSV headers must be " + ",".join(CSV_COLUMNS))
        rows = []
        seen = set()
        for number, row in enumerate(reader, start=2):
            try:
                project_id = clean_project_id(row.get("project-id"))
                quota_type = clean_text(row.get("quota-type"),
                                        "quota-type", required=True)
                quota_id = clean_text(row.get("quota-id"),
                                      "quota-id", required=True)
                value = clean_quota_value(row.get("value"))
                key = (project_id, quota_type, quota_id)
                if key in seen:
                    raise ValidationError("duplicate project/quota row")
                seen.add(key)
                rows.append({"project_id": project_id,
                             "quota_type": quota_type,
                             "quota_id": quota_id,
                             "value": value})
            except ValidationError as e:
                raise ValidationError(f"row {number}: {e}") from e
        if not rows:
            raise ValidationError("CSV contains no data rows")
        if len(rows) > MAX_IMPORT_ROWS:
            raise ValidationError(f"CSV exceeds {MAX_IMPORT_ROWS} rows")
        return rows
    except (csv.Error, UnicodeError) as e:
        raise ValidationError(f"invalid CSV: {e}") from e


def import_quotas(cur, identity, text, allowed_idp_projects=None):
    """Upsert every valid CSV row or change nothing (transaction is owned by
    the caller). Triggers create one audit event for each changed row.

    ``allowed_idp_projects`` (a set of IdP project names), when not None,
    restricts the import to quotas of projects under those IdP projects
    (manager scoping). Admins pass None for unrestricted imports.
    """
    rows = parse_quota_csv(text)
    project_ids = sorted({row["project_id"] for row in rows})
    quota_types = sorted({row["quota_type"] for row in rows})
    cur.execute(
        "SELECT id FROM projects WHERE id = ANY(%s)",
        (project_ids,),
    )
    known_projects = {row[0] for row in cur.fetchall()}
    unknown = sorted(set(project_ids) - known_projects)
    if unknown:
        raise ValidationError("unknown project id: " + ", ".join(unknown))
    if allowed_idp_projects is not None:
        cur.execute(
            "SELECT id FROM projects WHERE idp_project = ANY(%s)",
            (sorted(allowed_idp_projects),),
        )
        scoped = {row[0] for row in cur.fetchall()}
        outside = sorted(set(project_ids) - scoped)
        if outside:
            raise ValidationError(
                "project id outside your managed projects: "
                + ", ".join(outside))
    cur.execute("SELECT id FROM quota_types WHERE id = ANY(%s) AND active",
                (quota_types,))
    known_types = {row[0] for row in cur.fetchall()}
    missing = sorted(set(quota_types) - known_types)
    if missing:
        raise ValidationError("unknown quota type: " + ", ".join(missing))

    batch_id = uuid.uuid4().hex
    # Row-level audit events are stamped by the trigger with this batch id
    # so project-scoped audit queries can correlate row changes with the
    # import batch summary event.
    cur.execute("SET LOCAL ycluster.import_batch_id = %s", (batch_id,))
    changed = 0
    for row in rows:
        cur.execute(
            """SELECT value FROM project_quotas
               WHERE project_id = %s AND quota_type = %s AND quota_id = %s
               FOR UPDATE""",
            (row["project_id"], row["quota_type"], row["quota_id"]),
        )
        old = cur.fetchone()
        if old and Decimal(str(old[0])) == row["value"]:
            continue
        cur.execute(
            """INSERT INTO project_quotas
               (project_id, quota_type, quota_id, value)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (project_id, quota_type, quota_id)
               DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
            (row["project_id"], row["quota_type"], row["quota_id"],
             row["value"]),
        )
        changed += 1
    actor = _actor(identity)
    cur.execute(
        """INSERT INTO audit_events
           (actor_uid, actor_email, action, table_name, entity_type,
            entity_id, before_data, after_data, request_id, import_batch_id)
           VALUES (%s, %s, 'import', 'project_quotas', 'import_batch', %s,
                   NULL, %s, %s, %s)""",
        (actor["uid"], actor["email"] or "", batch_id,
         json.dumps({"rows": len(rows), "changed": changed}),
         batch_id, batch_id),
    )
    return {"rows": len(rows), "changed": changed, "batch_id": batch_id}


def audit_log(cur, project_id=None, limit=200):
    limit = max(1, min(1000, int(limit)))
    if project_id:
        clean_project_id(project_id)
        # Match project-scoped events of all shapes: project rows
        # (after_data/before_data ->> 'id'), membership/quota rows
        # (entity_id = 'project/...'), and import batches (import_batch_id
        # is in after_data with entity_id = batch id).
        cur.execute(
            """SELECT id, ts, actor_uid, actor_email, action, table_name,
                      entity_type, entity_id, before_data, after_data,
                      request_id, import_batch_id
               FROM audit_events
               WHERE after_data ->> 'id' = %(pid)s
                  OR before_data ->> 'id' = %(pid)s
                  OR entity_id = %(pid)s
                  OR entity_id LIKE %(prefix)s
                  OR (action = 'import'
                      AND import_batch_id IN (SELECT import_batch_id
                                              FROM audit_events
                                              WHERE entity_id LIKE %(prefix)s
                                                OR entity_id = %(pid)s))
               ORDER BY id DESC LIMIT %(limit)s""",
            {'pid': project_id, 'prefix': project_id + '/%',
             'limit': limit},
        )
    else:
        cur.execute(
            """SELECT id, ts, actor_uid, actor_email, action, table_name,
                      entity_type, entity_id, before_data, after_data,
                      request_id, import_batch_id
               FROM audit_events ORDER BY id DESC LIMIT %s""",
            (limit,),
        )
    keys = ("id", "ts", "actor_uid", "actor_email", "action", "table_name",
            "entity_type", "entity_id", "before_data", "after_data",
            "request_id", "import_batch_id")
    return [dict(zip(keys, row)) for row in cur.fetchall()]
