# Dev cluster system tests (reference)

Loaded when the `testing` skill is active and the change is touching the
dev cluster (cross-component integration) — not for unit-test work.

## Lifecycle

The dev cluster is a throwaway 5-node Incus cluster (`s1-s3` core,
`c1` compute, `f1` frontend). Provisioning is idempotent and uses a
shared `rbd-backing` volume to simulate Ceph — postgres/qdrant/
registry/authentik state follows leadership exactly like the prod RBD
maps.

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

**Rootless host prep comes first (once, or after every reboot).** The dev
cluster runs rootless by default (`SUDO` empty in `dev-cluster.sh`), which
attaches every node to a pre-created per-user bridge `incusbr-<uid>` on
`10.0.0.0/16` (NAT on, DHCP off) plus a `yc-dev-firewall` egress oneshot.
That bridge + firewall are created by **`./dev-cluster.sh setup`** (sudo;
the `just setup-rootless-incus` target is a thin alias). Without it the
nodes get a static `10.0.0.x/24` but the bridge is on the wrong subnet
(e.g. Incus's auto-created `10.90.109.1/24`), so nothing routes out:
provisioning hangs on the apt cache with `Temporary failure resolving
'archive.ubuntu.com'`. See Troubleshooting below. `setup` also detaches the
leftover `ycdev0` bridge from the old `SUDO=sudo` path and passes `$(id -u)`
explicitly (the underlying script defaults to a `dev` user some hosts lack).

**Always run `system-test.sh` in the background with a log file** — a
full run takes ~5 min and produces thousands of lines:

```bash
mkdir -p tmp
./system-test.sh --background --no-disruptive 2>&1 > tmp/system-test.log
tail -f tmp/system-test.log
grep -E '^(== SECTION|PASS|FAIL)' tmp/system-test.log
```

Sections run **independently** — a failed section is recorded and the
run continues. Exit code is non-zero if any section failed. Per-section
playbook logs land at `./tmp/system-test-<playbook>.log` (repo-root
`tmp/`, gitignored).

SSH into nodes via the generated config (no setup needed; entries
auto-resolve to incus containers):

```bash
ssh -F dev/cluster/.ssh/config s1 'systemctl status postgresql@16-main'
```

## Sections

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
| 14 | local-ai-proxy | all storage nodes run it, `ycluster inference add` hot-reloads, chat completion proxied through nginx auth, missing/wrong bearer → 401, wrong token on a live per-user key → clean 401 (argon2 verify-mismatch path) |
| 15 | ACME via Pebble | Pebble directory via `pebble.xc`, certbot obtain against pebble, certificate in etcd, nginx restored to dev local mode |
| 16 | leadership + VIP failover | drain leader → leadership moves → VIP follows → registry/authentik re-bound → state preserved → failback — **disruptive** |
| 17 | backup + verify-restore | encrypted backups produced, verify-restore loaded into scratch, metrics report success |

Deliberately **not** covered (per `system-test.sh` header): Ceph,
PXE/autoinstall boot path, GPU passthrough, Open-WebUI, monitoring
stack. These can't run in containers or on a laptop.

### Running one section

Pass `-s` / `--section` to `system-test.sh` to run a subset; provisioning
still runs when not `--assert-only`, so it stays a true end-to-end check:

```bash
./system-test.sh -s 15                  # run only section 15
./system-test.sh -s 1-5,15              # sections 1-5 and 15
./system-test.sh -s acme                # any section whose name contains acme
./system-test.sh --background -s 14     # background, only local-ai-proxy
```

Use `-s 14` (local-ai-proxy) to validate auth changes without the ~5 min
full run. Each section is a `sec_xxx()` function; the registry near the
bottom of `system-test.sh` maps `"<num>.<name>|sec_xxx"`.

## When dev cluster tests won't see your change

The repo is not mounted in the containers — code lives on the cluster
nodes at `/opt/infrastructure/` (managed by `dev-sync` on prod, by
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
  python3-protobuf`, or use a venv with them (`./.venv/bin/python` is
  auto-detected by `run-tests.sh`).
- **Provisioning hangs: aptcache/node can't resolve or reach the internet**
  — `Temporary failure resolving 'archive.ubuntu.com'`, `ping 10.0.0.1`
  (the node's gateway) lost, but `incus list` shows the node with a static
  `10.0.0.x` address. This is the rootless bridge misconfigured, not a
  DNS problem: `dev-cluster.sh` rootless mode trusts `incusbr-$(id -u)`,
  so if `setup-rootless-incus.sh` wasn't run (or the bridge was
  auto-created by Incus on the wrong subnet like `10.90.109.1/24`), there's
  no NAT/route for the `10.0.0.0/24` source range. Fix with
  `./dev/cluster/dev-cluster.sh setup` (sudo), then re-run. From the
  restricted user project you can't reconfigure the bridge (`Error: User
  does not have permission for project "default"`), so sudo is required.
- **Section fails mid-run** — section logs at `./tmp/system-test-<playbook>.log`.
  SSH into the failing node via `dev/cluster/.ssh/config` to inspect
  (e.g. `ssh -F dev/cluster/.ssh/config s1 'journalctl -u postgresql@16-main -n 50'`).
- **`just test-system` fails** with "binary not found" — needs
  `make build` first (in `local-ai-proxy/`); the test invokes `bin/local-ai-proxy`.
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
