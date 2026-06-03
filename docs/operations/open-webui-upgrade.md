# Open-WebUI Upgrade

How to upgrade the Open-WebUI deployment to a new upstream version
using the `stage` instance as a dress rehearsal before touching prod.

## Layout recap

Two instances run on the storage leader, fully isolated:

| Instance | Container       | Port | Postgres DB        | Data dir                      | Image tag |
|----------|-----------------|------|--------------------|-------------------------------|-----------|
| prod     | open-webui      | 8380 | `openwebui`        | `/rbd/misc/app/open-webui-data`       | `:latest` |
| stage    | open-webui-stage| 8381 | `openwebui-stage`  | `/rbd/misc/app-stage/open-webui-data` | `:stage`  |

Postgres runs natively on the storage leader (not in a container).
Both DBs live in the same cluster, so cloning is a local
`pg_dump | psql`.

The build playbook checks out `webui_repo` into
`/opt/src/open-webui-build{,-stage}/open-webui` and builds an image
into the cluster registry.

## Approach

1. Research the upstream version range to understand schema and
   breaking changes.
2. Clone prod's DB into stage's DB (and optionally the RBD data
   dir) so the stage run exercises the real migration path against
   real data.
3. Build the target version as `:stage` and let it migrate the
   cloned DB on startup.
4. Kick the tires on stage.
5. Decide whether prod cutover is the same image or needs further
   work.

## Research checklist

Before touching anything, capture:

- Currently deployed version. `package.json` only tells you the
  frontend version — the source of truth is the alembic head:
  ```bash
  ssh s2.yc 'docker exec open-webui sh -c "cd /app/backend && \
    python -c \"from open_webui.internal.db import engine; \
    from sqlalchemy import text; \
    print(list(engine.connect().execute(text(\\\"select version_num from alembic_version\\\"))))\""'
  ```
  Cross-reference the revision id against
  `backend/open_webui/migrations/versions/` upstream to find the
  matching release.
- Latest upstream release tag from
  `github.com/open-webui/open-webui`.
- Every Alembic migration added in the range. Look for:
  - destructive ops (column drops, table rebuilds, PK changes)
  - data backfills (slow on large DBs)
  - `NOT NULL` adds without defaults
  - renames
- Release notes for breaking changes: env var renames, DB driver
  swaps (e.g. asyncpg → psycopg), removed endpoints, plugin API
  changes.
- Whether the `api_key` table schema changes — `local-ai-proxy-auth`
  validates bearer tokens against it directly, so a schema change
  there needs coordinated work.

## Upgrade steps

### 1. Stop stage

```bash
ssh s2.yc systemctl stop open-webui-stage
```

### 2. Clone prod DB into stage DB

Run on the storage leader (s2 at time of writing).

**2a. Dump prod.** The redirect runs as the root shell, so the dump
file lands in `/rbd/misc/backups/` regardless of its perms:

```bash
DUMP=/rbd/misc/backups/owui-prod-$(date +%Y%m%d-%H%M%S).sql
sudo -u postgres pg_dump --no-owner --no-acl openwebui > "$DUMP"
```

`--no-owner --no-acl` makes the dump portable across the two app
users.

**2b. Drop & recreate stage DB.** The container is already stopped
(step 1), but if `dropdb` complains about active connections,
something else is holding one open — investigate, don't just force.

```bash
sudo -u postgres dropdb openwebui-stage
sudo -u postgres createdb -O "openwebui-stage" openwebui-stage
```

**2c. Restore.** Pipe the dump through stdin — `/rbd/misc/backups/`
is mode 700 root, so the `postgres` user can't read files inside
it directly. The root shell reads, pipes to `psql`:

```bash
cat "$DUMP" | sudo -u postgres psql -v ON_ERROR_STOP=1 -d openwebui-stage
```

**2d. Fix object ownership.** Because the dump restored as
`postgres`, every object is owned by `postgres`. `REASSIGN OWNED BY
postgres` doesn't work — it errors on system-catalog objects also
owned by `postgres`. Reassign per-object in the `public` schema
instead:

```bash
sudo -u postgres psql -v ON_ERROR_STOP=1 -d openwebui-stage <<'SQL'
ALTER SCHEMA public OWNER TO "openwebui-stage";
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT schemaname, tablename FROM pg_tables
           WHERE schemaname = 'public' LOOP
    EXECUTE format('ALTER TABLE %I.%I OWNER TO %I',
                   r.schemaname, r.tablename, 'openwebui-stage');
  END LOOP;
  FOR r IN SELECT sequence_schema, sequence_name
           FROM information_schema.sequences
           WHERE sequence_schema = 'public' LOOP
    EXECUTE format('ALTER SEQUENCE %I.%I OWNER TO %I',
                   r.sequence_schema, r.sequence_name, 'openwebui-stage');
  END LOOP;
  FOR r IN SELECT n.nspname AS schemaname, c.relname AS viewname
           FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE c.relkind IN ('v','m') AND n.nspname = 'public' LOOP
    EXECUTE format('ALTER VIEW %I.%I OWNER TO %I',
                   r.schemaname, r.viewname, 'openwebui-stage');
  END LOOP;
END $$;
SQL
```

**2e. Verify.** Row counts and alembic head should match prod:

```bash
for db in openwebui openwebui-stage; do
  echo "=== $db ==="
  sudo -u postgres psql -d "$db" -c \
    "SELECT (SELECT count(*) FROM chat) chats,
            (SELECT count(*) FROM \"user\") users,
            (SELECT count(*) FROM api_key) api_keys,
            (SELECT version_num FROM alembic_version) alembic;"
done
```

### 3. (Optional) Clone the RBD data dir

The Postgres DB references files and embeddings that live under
the data dir (chroma vectors, uploads, etc.). If you skip this,
RAG / uploads / chat-attachment views in stage will 404 on rows
that came from prod. To make stage fully representative:

```bash
ssh s2.yc 'systemctl stop open-webui-stage'  # already stopped, just to be safe
ssh s2.yc 'rsync -aH --delete \
  /rbd/misc/app/open-webui-data/ \
  /rbd/misc/app-stage/open-webui-data/'
```

### 4. Point the build at the target version

Edit `config/ansible/app/build-open-webui.yml`:

- set `webui_repo` to the repo you want
  (`https://github.com/open-webui/open-webui.git` for upstream)
- pin the target version on the git task
  (`version: vX.Y.Z`, plus `force: yes` so an existing checkout
  is repointed cleanly)

If you're switching repos, the existing checkout in
`/opt/src/open-webui-build-stage/open-webui` may have a different
`origin` remote. `force: yes` handles the reset, but if the git
task fails, blow away the checkout and re-run.

### 5. Build the `:stage` image

```bash
ssh s3.yc 'cd /etc/ansible && \
  ./run-playbook.sh app/open-webui-stage.yml --tags build,never'
```

### 6. Start stage and watch the migration

```bash
ssh s2.yc systemctl start open-webui-stage
ssh s2.yc journalctl -u open-webui-stage -f
```

Migrations run on container start. Confirm the alembic head
matches what you expect for the target version:

```bash
ssh s2.yc 'docker exec open-webui-stage sh -c "cd /app/backend && \
  python -c \"from open_webui.internal.db import engine; \
  from sqlalchemy import text; \
  print(list(engine.connect().execute(text(\\\"select version_num from alembic_version\\\"))))\""'
```

### 7. Kick the tires on stage (port 8381)

- log in as a real user from the cloned DB
- send a chat through a model wired to `inference.xc`
- exercise API key auth — hit `/v1/` with a real `api_key` row's
  token (this is what `local-ai-proxy-auth` validates against)
- model list, admin settings
- if data dir was cloned: RAG, uploads, attachments
- anything called out in the release notes (e.g. prompts UI if the
  `prompt` table schema changed)
- watch logs for deprecation / config warnings, especially
  `DATABASE_URL` parsing and removed env vars

### 8. Prod cutover

Once stage is happy:

```bash
# Backup prod DB
ssh s2.yc 'sudo -u postgres pg_dump --no-owner --no-acl openwebui \
  > /rbd/misc/backups/owui-prod-precutover-$(date +%Y%m%d-%H%M%S).sql'

# Build :latest with the same target version
ssh s3.yc 'cd /etc/ansible && \
  ./run-playbook.sh app/build-open-webui.yml'

# Cutover
ssh s2.yc systemctl restart open-webui
ssh s2.yc journalctl -u open-webui -f
```

### If migration is interrupted mid-flight

If the container is stopped while alembic is running (signal, manual
restart, ycluster-apps.target churn), the open transaction rolls
back cleanly (alembic uses transactional DDL) — but the killed
Python process may leave an **orphaned Postgres backend** still
holding the lock from its half-run DDL. The next container start
will hang on `Running upgrade ... -> ...` with no further log
output, blocked on a relation lock.

Diagnose:

```bash
ssh s2.yc 'sudo -u postgres psql -d openwebui -c "
  SELECT pid, state, wait_event_type, wait_event,
         age(clock_timestamp(), query_start) AS age,
         substring(query, 1, 100)
  FROM pg_stat_activity
  WHERE datname = '\''openwebui'\'' AND state != '\''idle'\'';"'
```

Look for a long-`age` backend holding `Lock`/`relation` with an
`ALTER TABLE ...` query — that's the orphan. Terminate it:

```bash
ssh s2.yc 'sudo -u postgres psql -d openwebui -c \
  "SELECT pg_terminate_backend(<PID>);"'
```

The blocked migration will then proceed within seconds. Sanity-check
the alembic head and row counts afterwards.

### Rollback

Additive migrations have downgrade scripts, but anything that
drops columns is not safely reversible via `alembic downgrade` —
the data is gone. The honest rollback path is:

1. Stop the container.
2. Restore the pre-cutover Postgres dump into `openwebui`.
3. Repoint the build at the previous version and rebuild
   `:latest`.
4. Restart.

Keep the pre-cutover dump until you're confident in the new
version.
