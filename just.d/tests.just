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
#   just setup-rootless-incus
#                          one-time root host prep so the dev cluster can run
#                          rootless (per-user bridge on 10.0.0.0/16 + egress
#                          firewall). Requires sudo. Re-run after a reboot.
#                          Alias for ./dev/cluster/dev-cluster.sh setup.
#
# Logs land in ./tmp/ (gitignored). Watch with `tail -f ./tmp/<file>.log`.

TEST_LOG := './tmp/system-test.log'

test:
    @echo "== test: Python =="
    @./run-tests.sh
    @echo "== test: Go =="
    @make -s -C local-ai-proxy test
    @echo "== test: Go (ids-forwarder) =="
    @go test -C ids-forwarder ./...

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

test-system-fresh: clear-dev-cluster
    @mkdir -p ./tmp
    ./dev/cluster/system-test.sh --background
    @echo
    @echo "log: {{TEST_LOG}}  (tail -f to watch; grep -E '^(== SECTION|PASS|FAIL)' for summary)"
# Privileged, one-time host prep for rootless dev-cluster runs. Thin alias to
# ./dev/cluster/dev-cluster.sh setup, which owns the logic (clears a leftover
# ycdev0 bridge, then reconciles incusbr-<uid> onto 10.0.0.0/16 + installs the
# yc-dev-firewall egress rules).
setup-rootless-incus:
    ./dev/cluster/dev-cluster.sh setup
