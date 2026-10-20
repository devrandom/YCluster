# Admin database migrations

The admin API's PostgreSQL database (`ycluster`) is provisioned by
`provision-admin`, which `admin-projects.yml` runs on the storage leader.
Schema changes are versioned SQL files in
[`config/ansible/admin/files/migrations/`](../../config/ansible/admin/files/migrations/).

## How it works

- Files are named `<version>_<slug>.sql`, for example `0001_baseline.sql`.
  Versions must be unique and zero-padded so lexicographic order matches
  apply order.
- Each migration runs in its own transaction. A failed migration rolls back and
  stops the run.
- Applied versions are recorded in `schema_migrations` with the SHA-256 of the
  file. Editing a migration after it has been applied makes the next
  provisioning run fail instead of silently diverging from the database. A
  migration file that is missing on disk while recorded in the database, or an
  applied set that is not a prefix of the available versions, also fails.
- A session-level advisory lock serializes concurrent provisioning runs.
- The app role (`ycluster`) has no privileges on `schema_migrations`; only the
  provisioning role can read or write it.
- Grants are not part of migrations. `provision-admin` applies them after the
  migrations, and `audit_events` remains append-only at both the privilege and
  trigger level.

`0001_baseline.sql` captures the pre-framework schema. It is deliberately
idempotent (`CREATE TABLE IF NOT EXISTS`, `CREATE OR REPLACE FUNCTION`,
`DROP TRIGGER IF EXISTS`) so it can bootstrap a fresh database and also record
itself on databases created by the old provisioning script.

## Adding a migration

1. Add the next numbered file, for example
   `config/ansible/admin/files/migrations/0002_hardware_log.sql`.
2. Keep it additive where possible. Use `IF NOT EXISTS` for new objects.
3. Do not edit or rename an applied migration. Add a new one instead.
4. Re-run the playbook on the storage leader:

   ```
   cd /etc/ansible && ./run-playbook.sh admin/admin-projects.yml --limit managed
   ```

The playbook copies the migration directory to
`/usr/local/lib/ycluster/migrations` and then runs `provision-admin`.
Set `ADMIN_MIGRATIONS_DIR` to override the location for local runs or tests.

## Scope

Migrations cover schema and seed data. Attachments do not belong here: large
photos or invoices belong in object storage, with a reference recorded in the
database.
