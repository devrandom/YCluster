#!/usr/bin/env bash
# Run all Python unit tests: vm_manager domain/DAL logic + admin web app.
# Both suites are server-free (etcd/postgres faked at the DAL seam) so they
# run locally and on a cluster node alike.
#
#   ./run-tests.sh            # uses ./venv/bin/python if present, else python3
#   PYTHON=python3 ./run-tests.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILES="$ROOT/config/ansible/admin/files"
PY="${PYTHON:-}"
[ -z "$PY" ] && PY="$([ -x "$ROOT/venv/bin/python" ] && echo "$ROOT/venv/bin/python" || echo python3)"

echo "== vm_manager (domain + DAL) tests =="
( cd "$FILES/ycluster" && PYTHONPATH=. "$PY" -m unittest discover -s tests )

echo "== admin web-app tests =="
( cd "$FILES" && PYTHONPATH=".:ycluster" "$PY" -m unittest discover -s tests )

echo "All tests passed."
