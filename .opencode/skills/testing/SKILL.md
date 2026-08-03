---
name: testing
description: Run unit tests (Python + Go, server-free) and dev-cluster system tests (17 sections) — pick the right surface for the change
---

# Testing

Three test surfaces, ordered by isolation cost. Pick the narrowest one
that exercises the change; reserve the dev cluster for cross-component
integration seams (it's slow and only sees changes after a provisioning
step or a manual rsync/playbook re-run).

| Layer | What | Run time | Setup |
|---|---|---|---|
| Python + Go unit | server-free | <30s | none |
| Go system (tagged) | proxy subprocess + real HTTP backends | seconds | `make build` |
| Dev cluster system | end-to-end (17 sections) | minutes | Incus containers |

## Day-to-day (preferred)

```bash
just test              # Python + Go unit, ~30s — run during the dev cycle and before every commit
./dev/cluster/dev-cluster.sh setup  # one-time root host prep (just setup-rootless-incus alias)
just test-system       # dev cluster, --assert-only, background
just test-system-fresh # dev cluster with full provisioning (slow first time)
```

`just test` runs `./run-tests.sh` (both Python suites) and
`make -C local-ai-proxy test` (Go unit). Use it as the default before
every commit; reserve `just test-system*` for cross-component seams.

**Rootless host prep is a hard prerequisite for the dev cluster.** The
per-user bridge `incusbr-<uid>` (on `10.0.0.0/16`, NAT on, DHCP off) and
the `yc-dev-firewall` egress rules are created by
`./dev/cluster/dev-cluster.sh setup` (sudo; `just setup-rootless-incus`
is an alias). If the bridge wasn't set up the nodes get no outbound
network: provisioning hangs on the apt cache with `Temporary failure
resolving 'archive.ubuntu.com'`. See `references/system-test.md` for the
exact failure mode.

For the dev-cluster lifecycle, the 17-section list, troubleshooting,
and the deploy→test workflow when dev nodes won't see your change,
see `references/system-test.md`.

## Python unit (what `just test` runs)

The ycluster package's `__init__.py` imports `etcd3`, so each `tests/`
directory deliberately has **no `__init__.py`** — use module paths,
not package-name `discover`:

```bash
./run-tests.sh                                          # both suites
( cd config/ansible/admin/files/ycluster && \
  PYTHONPATH=. python3 -m unittest discover -s tests )  # ycluster package tests only
( cd config/ansible/admin/files && \
  PYTHONPATH=".:ycluster" python3 -m unittest discover -s tests )  # admin web tests only
```

`run-tests.sh` auto-uses `./.venv/bin/python` when present, otherwise
falls back to `python3` (which needs the apt deps installed — see the
references file if `etcd3` fails to import).

## Go unit (what `just test` runs)

```bash
cd local-ai-proxy
make test           # default; skips system-tagged
make test-race      # with -race
make test-synctest  # GOEXPERIMENT=synctest, Go 1.24 only
make test-system    # tagged; needs `make build` first
make check          # verify + vet + test + vulncheck
```
