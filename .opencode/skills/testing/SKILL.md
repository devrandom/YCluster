---
name: testing
description: Run unit tests (Python + Go, server-free) and dev-cluster system tests (17 sections) — pick the right surface for the change
---

# Testing

Three test surfaces, ordered by isolation cost:

| Layer | What | Where | Run time | Setup |
|---|---|---|---|---|
| Python unit (ycluster pkg) | domain logic, pure functions | `config/ansible/admin/files/ycluster/tests/` | <1s | none (deps are apt-installed) |
| Python unit (admin web) | Flask endpoints via `FakeEtcd` | `config/ansible/admin/files/tests/` | <1s | none |
| Go unit | router/handler/health/etc. | `local-ai-proxy/*_test.go` | <1s | none |
| Go system (tagged) | proxy as subprocess + real HTTP backends | `local-ai-proxy/system_test.go` | seconds | `make build` |
| Dev cluster system | end-to-end integration | `dev/cluster/system-test.sh` | minutes | Incus containers |

Pick the narrowest surface that exercises the change. Reserve the dev
cluster for cross-component integration seams; it's slow and edits
on the dev nodes only become visible after a `dev/cluster/system-test.sh`
provision step or a manual `rsync`/playbook re-run.

## Python unit tests

The ycluster package's `__init__.py` imports `etcd3`, so each
`tests/` directory deliberately has **no `__init__.py`** — use module
paths, not package-name `discover`:

```bash
# both suites at once (uses ./venv/bin/python if present)
./run-tests.sh

# ycluster package tests only (4 files: backup_status, clock_skew,
# gateway_health, vm_scheduling)
( cd config/ansible/admin/files/ycluster && \
  PYTHONPATH=. python3 -m unittest discover -s tests )

# admin web-app tests (test_app.py — drives the Flask app through its
# test client; etcd is faked at the vm_manager DAL seam via fake_etcd.py)
( cd config/ansible/admin/files && \
  PYTHONPATH=".:ycluster" python3 -m unittest discover -s tests )

# single module / single test
( cd config/ansible/admin/files/ycluster && \
  PYTHONPATH=. python3 -m unittest tests.test_clock_skew )
```

`test_app.py` covers the schedule endpoints (admission control, owner
checks, conflict logic). The pure-function tests under
`ycluster/tests/` are intentionally self-contained — many use
`importlib.util.spec_from_file_location` to load modules by file path
so they don't pull in the package's `__init__`.

Deps come from apt (`python3-etcd3`, `python3-flask`, `python3-requests`,
`python3-protobuf`); `run-tests.sh` auto-uses `./venv/bin/python` when
present, otherwise falls back to `python3` (which needs the apt deps
installed to even import).

## Go unit tests

```bash
cd local-ai-proxy
make test            # default: all *_test.go except system-tagged
make test-race       # same with -race
make test-synctest   # GOEXPERIMENT=synctest (Go 1.24 only)
make test-system     # tagged: real HTTP backends + proxy subprocess
make check           # verify + vet + test + vulncheck
```

`system_test.go` is gated by `//go:build system` so the default `test`
target skips it. `make test-system` requires `make build` first — the
tests invoke `bin/local-ai-proxy` as a subprocess and wait for
`/healthz` to come up.

## Dev cluster system tests

The dev cluster is the throwaway 5-node Incus cluster (`s1-s3` core,
`c1` compute, `f1` frontend). Provisioning is idempotent and uses a
shared `rbd-backing` volume to simulate Ceph — postgres/qdrant/registry/
authentik state follows leadership exactly like the prod RBD maps.

```bash
cd dev/cluster

./dev-cluster.sh up                # incus init + 5 nodes + apt cache (slow first time)
./dev-cluster.sh status            # list nodes
./dev-cluster.sh reset             # down + recreate (warm apt cache)
./dev-cluster.sh purge             # also delete the apt cache

./system-test.sh                   # up + provision + 17 sections
./system-test.sh --assert-only     # skip provisioning, just re-run assertions
./system-test.sh --no-disruptive   # skip section 16 (leadership failover)
```

Sections run **independently** — a failed section is recorded and the
run continues (one broken seam doesn't hide the state of the others).
Exit code is non-zero if any section failed. Per-section playbook logs
land at `/tmp/system-test-<playbook>.log`.

SSH into nodes via the generated config (no setup needed; entries
auto-resolve to incus containers):

```bash
ssh -F dev/cluster/.ssh/config s1 'systemctl status postgresql@16-main'
```

### Sections

| # | Section | Asserts |
|---|---|---|
| 1 | substrate + etcd quorum (mTLS) | etcd up across s1-s3, mTLS enforced, plaintext :2379 refused, quorum write/read |
| 2 | ycluster CLI round-trips | `cluster status`, `tls`/`healthchecks`/`backup`/`dhcp` CRUD |
| 3 | frontend management | `ycluster frontend add/show/list/delete`; f1 registered |
| 4 | admin-api (core + non-core) | `/api/status` via nginx on c1, `/api/health`, `/metrics`, `/api/allocate` (etcd write), `/api/inventory` |
| 5 | base infra | dnsmasq+squid/chrony on s1-s3 + c1, gateway VIP on exactly one node, cluster DNS serves service names, `/api/health` healthy on c1 |
| 6 | adhoc DHCP join | incus `x1` DHCPs `10.0.0.151`, registers in etcd, resolves as `x1.xc` |
| 7 | storage leader + VIP | `/cluster/leader/app` set, `/rbd/{user,misc}` mounted, leader-only services (postgres/registry/rathole/authentik/qdrant/rbd) active, VIP on leader only |
| 8 | DHCP leader election | `/cluster/leader/dhcp` set, fails over on `systemctl stop/start dhcp-leader-election` — **disruptive** |
| 9 | postgres + authentik + user mgmt | authentik DB, `/-/health/live/`, `ycluster user add/admin/invite/uninvite` (CLI → IdP API → postgres) |
| 10 | incus VM lifecycle (c1) | sampler+reconciler timers, `vm launch`, `vm sample`, grace-stamp+stop on desired=off, start on desired=on, `vm destroy` |
| 11 | usage accounting → postgres | `vm_events` drained, `/cluster/vms-events/` empty, `vm_samples` rows |
| 12 | rathole tunnels via f1 | ssh banners through f1:2201-2203, http via f1:80 → leader public vhost |
| 13 | admin web forward-auth | anonymous `/admin/` → 302 to authentik outpost; `/api/inventory` open internally, gated publicly |
| 14 | local-ai-proxy | all storage nodes run it, `ycluster inference add` hot-reloads, chat completion proxied through nginx auth, missing/wrong bearer → 401 |
| 15 | ACME via Pebble | Pebble directory via `pebble.xc`, certbot obtain against pebble, certificate in etcd, nginx restored to dev local mode |
| 16 | leadership + VIP failover | drain leader → leadership moves → VIP follows → registry/authentik re-bound → state preserved → failback — **disruptive** |
| 17 | backup + verify-restore | encrypted backups produced, verify-restore loaded into scratch, metrics report success |

Deliberately **not** covered (per `system-test.sh` header): Ceph,
PXE/autoinstall boot path, GPU passthrough, Open-WebUI, monitoring
stack. These can't run in containers or on a laptop.

### Running one section

There's no `--only-section` flag. To re-run a single section, edit
`main()` in `system-test.sh` to comment out the other sections — or
use `--assert-only` and patch the script temporarily. Each section is
a `sec_xxx()` function; lines 818-834 in `system-test.sh` list them.

## When dev cluster tests won't see your change

The repo is not mounted in the containers — code lives on the cluster
nodes at `/opt/infrastructure/` (managed by `dev-sync.sh` on prod, by
the playbooks on dev). Two paths to update dev nodes:

1. **Use prod's deploy pattern**: commit, let the watcher sync, run
   the right playbook (see the deploy-to-cluster skill). Then
   `./system-test.sh --assert-only` to re-run assertions.
2. **Re-run the provisioning step** — `system-test.sh` without
   `--assert-only` re-runs `site-dev.yml` / `collect-hw-dev.yml` /
   `base-infra-dev.yml` / `app-dev.yml` / `acme-dev.yml`, which
   `git pull`s on each node and re-applies the relevant play.

For a Python change to `ycluster`, the playbook is
`admin/install-ycluster-package.yml --limit managed` (re-runs on the
dev cluster via `system-test.sh`'s `app-dev.yml`). For Flask/app.py
or templates, it's `admin/setup-web-services.yml --limit managed`.

## Troubleshooting

- **Python tests fail to import etcd3** — apt deps aren't installed
  locally. `apt install python3-etcd3 python3-flask python3-requests
  python3-protobuf`, or use a venv with them (`./venv/bin/python` is
  auto-detected by `run-tests.sh`).
- **Section fails mid-run** — section logs at `/tmp/system-test-<playbook>.log`.
  SSH into the failing node via `dev/cluster/.ssh/config` to inspect
  (e.g. `ssh -F dev/cluster/.ssh/config s1 'journalctl -u postgresql@16-main -n 50'`).
- **`make test-system` fails** with "binary not found" — needs
  `make build` first; the test invokes `bin/local-ai-proxy`.
- **Dev cluster drift / partial state** — `dev-cluster.sh reset`
  (down + recreate, warm apt cache reused) or `purge` to nuke the
  cache too. State is volatile on the AppVM root, persistent on `/rw`
  (see `dev/cluster/dev-cluster.sh:73-94`).
- **Section 16 left a stale drain** — section 1 clears stale drain
  keys via `etcdctl del /cluster/nodes/<n>/drain`; if section 1
  itself fails, manually clear with
  `ssh -F dev/cluster/.ssh/config s1 'etcdctl del --prefix /cluster/nodes/'`.
- **Pebble cert issues** — Pebble regenerates its CA on restart,
  orphaning certbot's cached ACME account; section 15 deletes only
  the pebble account dir (`/etc/letsencrypt/accounts/pebble.xc*`),
  never the real LE ones.
