#!/usr/bin/env python3
"""One-time migration: create a default project per Authentik user.

Every user gets a personal project named after their Authentik username and
that project is set as their default. The migration is idempotent: existing
projects/memberships are reused, never deleted, and safe to re-run.

The playbook calls this as root with `--from-authentik` to print user data
as JSON (root can read etcd client certs), then runs it again as the postgres
OS user with `--apply` — peer authentication then maps to the postgres DB
user, and all user data is passed through parameterized queries.

Intended to run before admins create user projects through the UI. If an
admin has already created a project with a colliding slug, the migration
adopts it as that user's default rather than failing — verify the mapping
is intended before running on a database with hand-created projects.
"""
import argparse
import json
import re
import sys

import psycopg2

ACTOR_UID = "migration"
ACTOR_EMAIL = ""

PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def slugify(value):
    slug = value.strip().lower().replace("@", "-").replace("_", "-")
    slug = re.sub(r"[^a-z0-9-]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    if not PROJECT_ID_RE.match(slug):
        raise Exception(f"cannot derive a valid project id from {value!r}")
    return slug


def db_connect():
    """Connect via peer auth; run as the postgres OS user for this."""
    return psycopg2.connect(dbname="ycluster", user="postgres")


def idp_project(user):
    """The user's IdP project, tolerating legacy list-shaped values."""
    project = user.get("project_name") or ""
    if isinstance(project, list):
        project = next((item for item in project
                        if str(item or "").strip()), "")
    return str(project).strip()


def migrate(conn, users):
    """Create one project per user (named by username) and set it default."""
    processed = 0
    changed = 0
    seen_project_ids = {}
    with conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL ycluster.actor_uid = %s", (ACTOR_UID,))
            cur.execute("SET LOCAL ycluster.actor_email = %s",
                        (ACTOR_EMAIL,))
            for user in users:
                username = (user.get("username") or "").strip()
                if not username:
                    continue
                project_id = slugify(username)
                if project_id in seen_project_ids:
                    raise Exception(
                        f"username {username!r} and "
                        f"{seen_project_ids[project_id]!r} both map to "
                        f"project id {project_id!r}; rename one user")
                seen_project_ids[project_id] = username
                cur.execute(
                    """INSERT INTO projects (id, name, idp_project)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (id) DO NOTHING""",
                    (project_id, username, idp_project(user)))
                cur.execute(
                    """UPDATE projects
                       SET idp_project = %s, updated_at = now()
                       WHERE id = %s AND idp_project = '' AND %s <> ''""",
                    (idp_project(user), project_id, idp_project(user)))

                # Never move an existing default: admins may have reassigned
                # it since the first run. Only seed users with no default.
                cur.execute(
                    """SELECT project_id FROM project_members
                       WHERE authentik_uid = %s AND is_default""",
                    (user["uid"],))
                if cur.fetchone() is not None:
                    processed += 1
                    continue

                email = user.get("email") or user["uid"]
                cur.execute(
                    """INSERT INTO project_members
                       (project_id, authentik_uid, email_at_assignment,
                        is_default)
                       VALUES (%s, %s, %s, TRUE)
                       ON CONFLICT (project_id, authentik_uid)
                       DO UPDATE SET is_default = TRUE""",
                    (project_id, user["uid"], email))
                processed += 1
                changed += 1
    return processed, changed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-authentik", action="store_true",
                        help="print Authentik users as JSON and exit")
    parser.add_argument("--apply", action="store_true",
                        help="read users JSON from stdin and apply")
    args = parser.parse_args()

    if args.from_authentik:
        from ycluster.utils import authentik_manager
        print(json.dumps(authentik_manager.users_data()))
        return

    if args.apply:
        users = json.load(sys.stdin)
        conn = db_connect()
        try:
            processed, changed = migrate(conn, users)
            print(f"migration changed: {changed} "
                  f"({processed} users processed)")
        finally:
            conn.close()
        return

    parser.error("specify --from-authentik or --apply")


if __name__ == "__main__":
    main()
