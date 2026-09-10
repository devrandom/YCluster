#!/usr/bin/env bash
# Run all Python unit tests: vm_manager domain/DAL logic + admin web app.
# Both suites are server-free (etcd/postgres faked at the DAL seam) so they
# run locally and on a cluster node alike.
#
#   ./run-tests.sh            # uses ./.venv/bin/python if present, else python3
#   PYTHON=python3 ./run-tests.sh
set -euo pipefail

# Keep diagnostic output on stdout so callers can distinguish "no stderr"
# from a failed run without losing warnings or failure details.
exec 2>&1
export PYTHONWARNINGS=ignore

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILES="$ROOT/config/ansible/admin/files"
PY="${PYTHON:-}"
[ -z "$PY" ] && PY="$([ -x "$ROOT/.venv/bin/python" ] && echo "$ROOT/.venv/bin/python" || echo python3)"

run_unittests() {
    local dir="$1" pythonpath="$2"
    (
        cd "$dir"
        PYTHONPATH="$pythonpath" "$PY" -c 'import logging; logging.getLogger().addHandler(logging.NullHandler()); import unittest; unittest.main(module=None, argv=["unittest", "discover", "-s", "tests", "--buffer"], exit=True)'
    )
}

echo "== vm_manager (domain + DAL) tests =="
run_unittests "$FILES/ycluster" .

echo "== admin web-app tests =="
run_unittests "$FILES" ".:ycluster"

echo "All tests passed."
