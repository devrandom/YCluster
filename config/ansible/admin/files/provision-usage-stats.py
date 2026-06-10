#!/usr/bin/env python3
"""Provision usage_stats database, user, and table on the PostgreSQL leader."""
import subprocess
import sys

from ycluster.common.etcd_utils import get_etcd_client


def get_password():
    client = get_etcd_client()
    result = client.get('/cluster/config/usage_stats/db-password')
    if result[0] is None:
        raise Exception('Password not found in etcd at /cluster/config/usage_stats/db-password')
    return result[0].decode()


def psql(cmd, db=None, check=False):
    # SQL arrives via stdin so it never touches a shell command line (no
    # quoting surface, not visible in ps). ON_ERROR_STOP keeps the return
    # code meaningful: without it psql exits 0 even when a statement fails.
    db_args = ['-d', db] if db else []
    result = subprocess.run(
        ['su', '-', 'postgres', '-c',
         'psql -v ON_ERROR_STOP=1 ' + ' '.join(db_args)],
        input=cmd, capture_output=True, text=True
    )
    if result.returncode != 0:
        ignored = any(e in result.stderr for e in ('already exists', 'already has', 'does not exist'))
        if not ignored:
            print(f"psql failed: {result.stderr}", file=sys.stderr)
            if check:
                sys.exit(1)
    return result


def main():
    password = get_password()

    # SQL-literal escape: a quote in the password must not break (or
    # truncate) the statement.
    pw_literal = password.replace("'", "''")
    psql(f"CREATE USER usage_stats WITH PASSWORD '{pw_literal}';")
    psql("CREATE DATABASE usage_stats OWNER usage_stats;")

    psql("""CREATE TABLE IF NOT EXISTS model_usage (
        id BIGSERIAL PRIMARY KEY,
        period_start TIMESTAMPTZ NOT NULL,
        period_end TIMESTAMPTZ NOT NULL,
        user_id TEXT NOT NULL,
        model TEXT NOT NULL,
        request_count BIGINT NOT NULL DEFAULT 0,
        total_duration_ms BIGINT NOT NULL DEFAULT 0,
        total_bytes_out BIGINT NOT NULL DEFAULT 0,
        UNIQUE(period_start, period_end, user_id, model)
    );""", db='usage_stats', check=True)

    psql("GRANT ALL PRIVILEGES ON DATABASE usage_stats TO usage_stats;", check=True)
    psql("GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO usage_stats;", db='usage_stats', check=True)
    psql("GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO usage_stats;", db='usage_stats', check=True)

    hba_path = '/etc/postgresql/16/main/pg_hba.conf'
    with open(hba_path) as f:
        content = f.read()
    for entry in (
        'host    usage_stats    usage_stats    10.0.0.0/24    scram-sha-256',
        'host    usage_stats    usage_stats    127.0.0.1/32    scram-sha-256',
    ):
        if entry not in content:
            with open(hba_path, 'a') as f:
                f.write('\n' + entry + '\n')
    if 'host    usage_stats' in content:
        subprocess.run(['systemctl', 'reload', 'postgresql@16-main'], capture_output=True)

    print('usage_stats provisioning complete')


if __name__ == '__main__':
    main()