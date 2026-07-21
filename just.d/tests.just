# Two test surfaces, ordered by isolation cost:
#
#   just test              server-free unit tests (Python + Go), ~30s.
#                          Use during a dev cycle, before every commit.
#
#   just test-system       dev-cluster system tests (background + log).
#                          Requires a running dev cluster (Incus containers
#                          s1-s3, c1, c2, f1). Re-runs the assertions only
#                          (--assert-only) — use test-system-fresh to also
#                          re-run the provisioning plays.
#
#   just test-system-fresh dev-cluster system tests with full provisioning
#                          (~5 min — site-dev + base-infra-dev + app-dev).
#
# Logs land in ./tmp/ (gitignored). Watch with `tail -f ./tmp/<file>.log`.

TEST_LOG := './tmp/system-test.log'

test:
    ./run-tests.sh
    @mkdir -p ./tmp/go-cache
    GOCACHE="$(pwd)/tmp/go-cache" make -C local-ai-proxy test

test-system-quick:
    @mkdir -p ./tmp
    ./dev/cluster/system-test.sh --assert-only

test-system:
    @mkdir -p ./tmp
    ./dev/cluster/system-test.sh --background
    @echo
    @echo "log: {{TEST_LOG}}  (tail -f to watch; grep -E '^(== SECTION|PASS|FAIL)' for summary)"

clear-dev-cluster:
    ./dev/cluster/dev-cluster.sh down

test-system-fresh: clear-dev-cluster test-system
