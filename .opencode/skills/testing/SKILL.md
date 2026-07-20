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

Day-to-day wrappers:

```bash
make test              # Python + Go unit, ~30s — run during the dev cycle and before every commit
make test-system       # dev cluster, --assert-only, background
make test-system-fresh # dev cluster with full provisioning (slow first time)
```

For the dev-cluster lifecycle, the 17-section list, troubleshooting,
and the deploy→test workflow when dev nodes won't see your change,
see `references/system-test.md`.

## Python unit

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

## Go unit

```bash
cd local-ai-proxy
make test           # default; skips system-tagged
make test-race      # with -race
make test-synctest  # GOEXPERIMENT=synctest, Go 1.24 only
make test-system    # tagged; needs `make build` first
make check          # verify + vet + test + vulncheck
```
