# TODO

## Priorities (triaged 2026-06-10)

A prioritized index into the detail below; nothing here is a separate item.

**Now (correctness/security with live impact or near-free fixes):**
- incus VM DNS records — DONE 2026-06-10: dev-tested, deployed to nv2/nv3/c1/c2, bastion resolution verified (details in item below).
- "core" consistency — main fixes DONE 2026-06-10 (rathole parametric per core node; static etcd/core floor + per-cluster inventory). Residual: `CORE_NODE_IPS` fallback + stale "s1-s3" comments (see item below).
- Review quick-win bugs B1-B8 — DONE 2026-06-10 (all eight fixed; canary-validated on s2, B2 also live-tested on the dev cluster; see review-followups section).
- Admin API hardening S1-S3 — highest security ROI: dev-server-as-root, unauthenticated mutating endpoints, unvalidated etcd-key params.
- Failed-systemd-units alert (Monitoring) — would have caught every breakage found this session.

**Next (operational resilience; caused real outages or data-loss exposure):**
- Gateway health-check hardening + s4 gateway-VIP eligibility — flapped the VIP on a 3-min WAN blip (2026-06-01).
- DR runbooks, esp. etcd quorum loss (only unrecoverable failure mode) + a `restore` path; Ceph off-cluster backup story.
- Incus storage `dir`→`zfs` migration — runaway-guest root-fs fill (nv3 cascade).
- WG inbound ingress that follows the leader; rathole PROXY protocol for real client IPs.
- Architecture A1-A4 (bootstrap trust model, plaintext keys, setup-time RBD `--exclusive`, Go-proxy watch resilience).

**Later (hygiene, docs, breadth):**
- Ansible hygiene H1-H4; remaining Monitoring gaps; Docker pruning; CI/lint.
- Dev-cluster system tests — script the incus DNS host-record verification as the first one (see item below).
- Node-lifecycle + scaling/capacity runbooks; secrets/SSH-key rotation; audit-log story; vendor-account cleanup.
- Minor review items; exo prompt-logging upstream; vllm-mlx operational gaps; MiniMax client workarounds doc.

---

- **P0: Give pinned incus VMs a permanent DNS record so name-based SSH (`ssh -J jump@<host>:<port> ubuntu@<vm>`) doesn't depend on a live DHCP lease.** incus only serves a DNS record for a VM while it holds a current `dnsmasq` lease; a static `ipv4.address` reservation pins the IP but creates no DNS record. A guest that lets its lease lapse between renewals (e.g. vm2 re-DISCOVERs ~every 90 min against a 1h lease) drops out of `dnsmasq.leases` for a recurring window, so the bastion's name lookup intermittently fails with "Temporary failure in name resolution" and external SSH breaks. Manually patched on nv2 2026-06-07 via `incus network set incusbr0 raw.dnsmasq` adding `host-record=vm2,vm2.incus,10.100.0.2` — but that lives only in nv2's running config and isn't recreated by `ycluster vm` IP-pinning. Durable fix: when `_pin_instance_ip` pins a VM's address (`config/ansible/admin/files/ycluster/ycluster/utils/vm_manager.py`), also write a matching `host-record` into the bridge's `raw.dnsmasq` (and reconcile/remove it on un-pin/delete), applied across all incus VM hosts (nv2, nv3, c1, c2). Consider whether incus `dns.mode`/static NIC DNS can do this natively instead of `raw.dnsmasq` surgery. **DONE 2026-06-10.** `sync_dns_records()` in vm_manager reconciles every managed bridge's `host-record=` lines from the eth0 pins (called from launch/destroy/pin-ips; `ycluster vm sync-dns` for backfill); `install-incus.yml`'s resolver task now preserves host-record lines instead of clobbering them (it would have wiped the manual nv2 patch on the next run). Ownership split: playbook owns resolver lines, vm_manager owns host-records. Verified on the local dev cluster (lease-less pinned containers resolve; add/remove lifecycle; both ownership directions idempotent), then deployed to nv2/nv3/c1/c2: nv2 regenerated vm2 + added the missing vm3 record, nv3 added vm1, c1/c2 no pinned instances; bastion `getent` resolves vm2/vm3 on nv2; `install-incus.yml --limit nv2` runs clean (resolver task `ok`, records intact).
- Turn the incus DNS host-record verification into an automated system test on the dev cluster. The 2026-06-10 fix was verified by hand against `dev/cluster/` (the dev nodes are pinned, never-DHCP containers — the exact lease-less case the fix targets); script it as a repeatable test, e.g. `dev/cluster/test-dns-sync.sh`: (1) `dev-cluster.sh up`; (2) baseline — clear `host-record=` lines from `ycdev0` `raw.dnsmasq`, assert `dig @10.0.0.1 s1.incus` is NXDOMAIN; (3) run `sync_dns_records()` (venv python, package from `config/ansible/admin/files/ycluster`), assert all pinned nodes resolve and a second run returns `{}`; (4) lifecycle — `incus init` a throwaway pinned instance, sync, assert it resolves; delete it, sync, assert NXDOMAIN with others intact; (5) ownership split — run the `install-incus.yml` resolver-task shell against `ycdev0`, assert host-records survive and the task is idempotent, then assert `sync_dns_records()` preserves the resolver lines; restore `raw.dnsmasq` at the end. This would be the first scripted system test on the dev cluster — structure it so further tests (etcd TLS migration rehearsal, leader election) can join a common harness (`dev/cluster/tests/`).
- Document MiniMax M2.5 client-side workarounds in `docs/operations/inference.md`: (a) use `/v1/completions` (not `/v1/chat/completions`) with a raw text prompt ending in the desired prefix for shaped output — the chat-completions `continue_final_message` flag is silently ignored by vllm-mlx 0.2.7; (b) client-side XML parsing of `<minimax:tool_call>` envelopes; (c) client-side truncation to emulate `stop`; (d) strip trailing `[e~[` from `.content` / `.text`. See `research/minimax-vllm-mlx-structured-output-2026-04-11.md`.
- vllm-mlx on m1 has a stack of upstream bugs and ad-hoc operational gaps (no `stop`, no guided decoding, broken special-token detokenization, no MiniMax tool-call parser, no pinned version, launch commands unscripted) — parked in `research/minimax-vllm-mlx-structured-output-2026-04-11.md` under "Upstream work queue" and "Operational gaps (m1)". Not actionable in the near term.
- Authenticate the "drain" function on the cluster status page
- Rebuild rathole with PROXY protocol support so the public admin-api (wg bootstrap + /status) sees real client IPs. Current rathole 0.5.0 is a plain L4 TCP tunnel — everything forwarded from the public host lands on 127.0.0.2 with source 127.0.0.1, so per-client rate limits and audit logs are impossible. nginx side would then use `listen 127.0.0.2:443 ssl proxy_protocol; set_real_ip_from 127.0.0.1; real_ip_header proxy_protocol;` once rathole emits the PP header. Upstream rathole doesn't support it — may need a fork/patch.
- Upstream the exo prompt-logging fix. exo leaks prompt content into its logs (and the on-disk log file) at INFO — `logger.info(prompt)` in `utils_mlx.py` plus `InputMessageContent.__repr__` showing the first 100 chars, which every task/command-object log dump recurses into. We carry a 2-hunk patch in `config/ansible/macos/files/exo/patches/0001-no-prompt-logging.patch`; submit it as a PR to exo-explore/exo so the carried patch can eventually be dropped.
- Harden the gateway uplink health check (`/usr/local/lib/cluster/check-gateway-health`). It currently fails the gateway VIP on a single HTTP GET to one external host (`detectportal.firefox.com`), so any brief ISP/DNS blip flaps the gateway VIP — and on 2026-06-01 21:41 a ~3-min upstream outage did exactly that, cascading into a storage-leadership failover and a cluster-wide WireGuard flap (nv1/nv2/nv3/c3 dropped). Improvements to investigate: (1) probe multiple targets and only fail if all are unreachable; (2) raw-IP ICMP/TCP fallback to the next hop / a known anycast IP so a DNS hiccup alone can't trip it; (3) require N consecutive failures (raise `fall`) and/or lengthen `interval` to ride out sub-minute stutters; (4) distinguish "uplink NIC/link down" (fail fast) from "internet unreachable" (tolerate longer) since they have different blast radii. Goal: a transient WAN stutter should not move the gateway VIP at all.
- s4 should not be eligible to hold the gateway VIP — it has no WAN uplink. s4 routes to the internet via the gateway VIP itself (`default via 10.0.0.254 dev enp1s0f0np0`) and has no `enp87s0`, yet it runs keepalived `VI_GATEWAY` with the *highest* base priority (104) and a `chk_gateway` that probes the nonexistent `enp87s0` (so it fails continuously, logged once on May 28 since keepalived only logs transitions). During a shared upstream outage where every node's check fails, s4 can still win the gateway VIP by raw priority math despite having no working internet path — this is what happened 2026-06-01 21:41:50 and it slows clean recovery. Fix: exclude s4 from the `VI_GATEWAY` instance entirely (only uplink-bearing nodes should run it), or give it priority 0 / a hard-fail check. Tie the gateway-VIP node set to "has an uplink interface" in the keepalived templating rather than to the `core` group.
- **s4 is intentionally drained — do not undrain until validated as a storage-leader.** s4 participates in Ceph/storage but has never been exercised as the storage leader (RBD mount of `/rbd/user`+`/rbd/misc`, postgres/qdrant/registry/rathole takeover, storage-VIP migration), so it's held out of leader election via `/cluster/nodes/s4/drain=true`. This is the correct steady state, not an anomaly — it was briefly undrained in error during the 2026-06-10 rolling reboot and re-drained. To make s4 leader-eligible: during a maintenance window, drain the current leader so leadership lands on s4, verify it mounts both RBD volumes cleanly, brings up all leader-only services, and that the storage VIP + external URL follow; only then leave it undrained. Overlaps the "core" consistency audit (is s4 core?) and the node-lifecycle runbook. Same s4 caveat as the gateway-VIP item above — s4 differs from s1-s3 in more than one subsystem.
- Consolidate redundant etcd reads in admin-api's health path (perf). On a core node, every `/metrics` scrape (frequent, per-node by Prometheus) makes ~6 separate etcd round-trips inside `get_comprehensive_health`: the `/test` probe, `is_storage_leader` (`/cluster/leader/app`), `is_dhcp_leader` (`/cluster/leader/dhcp`), `is_node_drained` (`/cluster/nodes/<h>/drain`), `check_certificate_expiry` (`/cluster/tls/cert`), and `get_core_nodes` → `get_all_hosts` (a full `by-hostname/` *prefix scan*). Fetch them in one batched/cached pass (e.g. a short TTL cache or a single multi-key read per scrape). Non-core nodes already make zero etcd calls here after the etcd-access-hardening Phase 1 gating; this is the core-node follow-up.
- Add periodic Docker image pruning on storage nodes (s1-s3). Dangling image layers from open-webui rebuilds accumulate in `/var/lib/docker/overlay2` and fill the root fs — on 2026-06-04 s2 hit `MON_DISK_LOW` (mon store lives on `/`, `/dev/mapper/vg0-root`) with ~50G of untagged `<none>` open-webui-with-plugins layers; `docker image prune -f` reclaimed it. Schedule a periodic `docker image prune -f` (dangling-only — never `-a`, which on a non-leader would delete the tagged-but-not-running open-webui `:latest` and force a ~4.4G re-pull on failover) via a systemd timer or Ansible-managed cron on each storage node. Note s2/s3 root disks (197G) are smaller than s1 (295G) so they hit the 30% `mon_data_avail_warn` threshold sooner. Registry blobs are unaffected by pruning — they live on RBD at `/rbd/misc/docker-registry/data`, outside Docker's image store.
- WG inbound ingress needs a stable externally-routable endpoint that follows the active leader. Currently the DNAT on the upstream router points at a specific core node's uplink IP (e.g. `192.168.0.104` = s3), so wg breaks on gateway-VIP failover to s1/s2. Options to investigate: (1) uplink-side VIP via keepalived on the 192.168.0.x segment — clean but only s2/s3 are on that subnet (s1 is on 192.168.9.x), so s1 can't participate and gateway-VIP leadership would need to be constrained to s2/s3; (2) static route `10.0.0.0/24 via <core node>` on the router so DNAT can target `10.0.0.254` directly — simplest but single-next-hop fragility; (3) a tiny UDP forwarder on each core node that knows which peer currently holds the gateway VIP and proxies wg packets there. Option 1 is probably the right call once s1's uplink is reconciled.

- **"core" definition consistency — residual cleanup.** The 2026-06-10 audit found three meanings of "core" and the two substantive ones are now resolved:
  - DONE — rathole SSH ingress made parametric per core node (`9169531`): server endpoints loop over `groups['core']`, client regex `^s(\d+)$`; s4 now has ssh4/2204, scales to s5+. Fixed the crash loop (was the `^s([123])$` hardcode in `rathole_config.py` vs `hosts: core` = s1-s4).
  - DONE — the `core`/`etcd` groups are now backed by a static FLOOR (s1-s3, general) in `inventory_boot.yml` + a gitignored per-cluster `inventory_cluster.yml` for extra core nodes / larger quorum (`2d60a12`, `4ffe153`). Fixes the DR/bootstrap circular dependency (recovery playbooks targeting `hosts: etcd` resolve when etcd is down) and leaves the door open to grow etcd to 5. The plugin still unions in dynamic s4+.
  - RESIDUAL: (1) **Tang server set hardcoded to s1-s3** (functional). `setup-tang.yml` runs on `hosts: storage` so it provisions a Tang server on every storage node *including s4*, but the Clevis SSS client binding only references `s1:8777 s2:8777 s3:8777` in three places — `storage/setup-user-rbd.yml:97-99`, `admin/files/scripts/secrets-volume-manager:37`, `storage/scripts/user-rbd-manager:43`. So s4's Tang server is unused and the unlock set won't grow with the cluster. Not breaking (2-of-3 over s1-3 works), but it gates LUKS unlock at boot, so any change needs careful testing (get it wrong → volumes don't unlock). Derive the Tang set from the etcd/quorum group instead of typing it three times. (2) `dhcp_server.py:48` `CORE_NODE_IPS` hardcodes s1-s3 → IP as a *fallback* used only when netplan parsing fails (s4+ still get their convention IP via the normal path, verified live); generalize to the `sN -> 10.0.0.10+N` convention. (3) Optional: have the plugin warn if live etcd membership disagrees with the static `etcd` group (drift cross-check). (Stale "s1-s3" comments/play-names fixed 2026-06-10.)

## Production readiness gaps (2026-05-22 audit)

### Disaster recovery & backups
- Write disaster-recovery runbooks for the cases the existing docs punt on: etcd quorum loss (minority survivors), Ceph quorum loss / corruption, full cluster cold-start, VIP split-brain recovery. `docs/operations/etcd.md` and `ceph.md` explicitly reference DR procedures that don't exist.
- Add a `restore` subcommand to `config/ansible/storage/scripts/backup-databases` (or sibling script) covering pg_dumpall, Qdrant snapshot, and `etcdctl snapshot restore`. Document the procedure end-to-end including age decryption.
- Periodic test-restore job: decrypt the latest backup to a scratch location and verify pg/qdrant/etcd load cleanly. Alert on failure or stale (>N day) backups. Two-layer approach to avoid widening attack surface:
  - **Nightly automated, on the storage leader.** Keep an age privkey on the storage leader (root-only, same FS as live pg/etcd data — putting it there doesn't expand blast radius since the host already holds plaintext) and restore the latest backup into a scratch pg cluster on a non-default port + a temp qdrant + an etcd snapshot dir. Smoke-query, then wipe. Caveat: enforce strict mode/ownership on the privkey file in the playbook (root:root 0400, on the same encrypted FS as the DB) — its "no extra surface" property depends on that.
  - **Quarterly attended drill with a hardware key.** Use `age-plugin-yubikey` (or TPM-backed) so the privkey never lives on disk anywhere. Operator plugs the token into a clean drill host, decrypts the latest offsite backup, restores end-to-end, runs smoke checks. This is what actually proves we can recover from compromise/loss of the storage leader. Token lives in a safe between drills; document key custody and a reissuance procedure for when an operator leaves.
- Decide on a Ceph backup story — currently RBD is the primary copy of `/rbd/user` etc. and isn't snapshotted off-cluster.
- Document age private-key custody (where the recipient privkeys live off-cluster, who can decrypt, rotation procedure).

### Node lifecycle
- End-to-end "replace a dead node" runbook: PXE → autoinstall → etcd rejoin → Ceph rejoin → service handoff, with explicit checkpoints.
- Graceful decommission runbook (drain Ceph OSDs, leave etcd, retire from inventory).
- `ycluster node {add,remove,drain}` CLI wrappers around the above so it's not a manual ssh+sudo dance during an outage.
- `ycluster failover` to manually move storage-leader / gateway VIP for maintenance.

### Monitoring gaps
- GPU health: temperature, ECC errors, memory, FLR events. The 2026-05-21 nv3 cascade (NOTES.md) wouldn't have been caught.
- Incus / VM guest disk-full alerts (root cause of the same cascade).
- Postgres and Qdrant liveness alerts (currently only etcd, Ceph, DHCP, inference are watched).
- Backup freshness / rsync-success metric + alert.
- Clock-skew alert rule (README claims it; not present in `ycluster-alerts.yml.j2`).
- Ansible-run-success metric so "did the last apply succeed on every host" isn't a manual SSH check.
- **Failed-systemd-units alert.** Every breakage found during the 2026-06-10 session (collect-model-stats, rathole, rathole-ssh, certbot-renew, wg-reconcile — all stale units hardcoded to plaintext etcd `:2379` after TLS enforcement) was a unit silently in `failed` state, discovered only by manual `journalctl` / a reboot test. node-exporter already exports `node_systemd_unit_state{state="failed"}`, so this is just an alert rule in `ycluster-alerts.yml.j2` (`== 1` for ~15m, per unit/node). Prerequisite: mask/fix the known-benign offenders first or they'll be permanent noise — `openipmi` done 2026-06-10 (`storage/disable-openipmi.yml`); still open on storage nodes: `tangd.socket`, `serial-getty@ttyS4`. Partly subsumes the per-service liveness and ansible-run-success items above.

### CI / testing
- ansible-lint in a GitHub Action gating `main`.
- `ansible-playbook --check` smoke run against a representative inventory subset.
- Pre-commit hook for yaml/jinja lint.

### Incus storage migration (urgent, from NOTES.md)
- Migrate Incus storage pools from `dir` to `zfs` with per-VM `refreservation` so a runaway guest can't fill the host root fs and trigger the vm1/GPU-FLR cascade seen on nv3 2026-05-21. Write the migration procedure (it's destructive — VMs get recreated).

### Known issues in NOTES.md to convert into tickets or runbook entries
- open-webui stops immediately after leader election (2025-10-09).
- meshcommander KVM/SOL broken; Ubuntu pass-change broken.
- Missing `docker.gpg.asc` on some hosts.
- Squid slow-DNS regression re-applied by Ansible after manual fix.
- ollama memory plugin failure with no graceful degradation.
- "redo s3" — clarify scope (Ceph RGW?) and either fix or file.

### Scaling / capacity docs
- Add-storage-node and add-compute-node procedures (Ceph rebalance expectations, OSD onboarding, network/VLAN, GPU setup where relevant).
- Capacity-planning notes: Ceph write-perf cliff at ~85% (empirical, in NOTES), recommended free-space margins, rebalance time estimates.

## Code review follow-ups (2026-06-09)

From `docs/reviews/2026-06-09-codebase-review.md` (IDs reference that doc; line numbers are vs commit f1e3305). Items already tracked elsewhere in this file are not duplicated here: drain-auth (top section + superseded by S2 below), CA-off-RBD (Security), rathole PROXY protocol, gateway health check, etcd read consolidation.

### Verified bugs (quick wins) — all DONE 2026-06-10
B1-B8 fixed and deployed: timing-safe+bytes compare (B1); allocation CAS result checked with retry/re-read (B2); DHCP allocation compares + in-transaction old-hostname delete + retry (B3); proxy retries only 5xx/429/404 (B4, 404 kept deliberately for model-placement drift); psql via stdin + ON_ERROR_STOP + escaped literal (B5); become fixed (B6); sha256 on Qdrant tarball (TOFU, binary matched deployed) and Ubuntu ISO both sources (B7); per-host warnings + broken-client reset in inventory plugin (B8).
- B1 — timing-unsafe master-key compare: `local-ai-proxy-auth.py:117` uses `token == master`; switch to `secrets.compare_digest`.
- B2 — allocation etcd transaction result ignored (`admin/files/app.py:326`, `failure=[]`, return unchecked); a compare failure returns an uncommitted/colliding hostname-IP. Check result, retry/raise.
- B3 — DHCP allocation check-then-use race (`utils/dhcp_server.py` ~:473-545, empty `compare=[]`); include `version(...)==0` compares.
- B4 — Go proxy retries every ≥400 on the next backend (`handler.go:324`); replays non-idempotent POSTs and masks client errors. Retry only 5xx/429.
- B5 — SQL string interpolation in `admin/files/provision-usage-stats.py:35`; parameterize.
- B6 — `wipe-etcd.yml:4` has `become: core` (invalid value); should be `become: true`.
- B7 — unverified downloads: Qdrant tarball (`storage/install-qdrant.yml`) and Ubuntu ISO (`admin/setup-pxe-boot.yml`) fetched with no checksum; add `checksum:`.
- B8 — bare `except:` in `inventory_plugins/etcd_nodes.py:132` yields a silent empty inventory; catch specific exceptions + warn per host.

### Admin API hardening (highest security ROI)
- S1 — admin-api runs Flask's dev server as root with no systemd hardening (`app.py:2721`, `setup-web-services.yml:67`). Move to gunicorn/waitress, dedicated user, `NoNewPrivileges`/`ProtectSystem=strict`.
- S2 — mutating admin-api endpoints unauthenticated (`/api/host/<h>/disable|enable`, drain, `/api/allocate`); anything on 10.0.0.0/24 can disable hosts or claim allocations. (Supersedes the standalone drain-auth item — validate against the etcd master key.)
- S3 — route params flow unvalidated into etcd keys (`f"{ETCD_PREFIX}/by-hostname/{hostname}"`); validate `^[a-z]+[0-9]+$` before use.

### Architecture
- A1 — bootstrap is trust-on-first-use keyed to MAC addresses (rogue LAN device can PXE-join as any node type; `/bootstrap/*` served unsigned to `sudo bash`). Document the LAN/TOFU trust model in ARCHITECTURE.md; cheap hardening: MAC-OUI validation + checksum-verified bootstrap scripts.
- A2 — API keys stored plaintext (etcd master key; Open-WebUI `api_key` rows). Threat-model item; hashing the OWUI keys needs upstream changes.
- A3 — setup-time RBD maps lack `--exclusive` (`storage/setup-user-rbd.yml:68`, `storage/setup-misc-rbd.yml:29`) though runtime mounts have it; add for consistency.
- A4 — Go proxy etcd-watch isn't restarted on break (`source.go:134`) so model config silently stops hot-reloading; disabled-backends set goes stale on etcd outage (`disabled.go:32`). Add watch-error → re-list-and-rewatch.

### Ansible hygiene
- H1 — `groups['storage'][0]` delegation (~20× in `storage/setup-user-rbd.yml`) breaks under `--limit`; use the mountpoint-based leader-detection convention.
- H2 — `ignore_errors: yes` on stop/teardown paths (`wipe-etcd.yml`, `storage/stop-storage-leader-election.yml`) hides hung services before destructive steps; use `failed_when:` with explicit checks.
- H3 — systemd units for the DHCP server and admin-api have no hardening directives even where root is required for raw sockets.
- H4 — `admin/install-vm-bastion.yml` reads the rathole token via etcdctl without `no_log` on the registering task.

### Minor
- `install-ycluster-package.yml` restarts admin-api when the package updates but not dhcp-server, so the DHCP leader keeps running old package code until something else restarts it (had to restart it by hand after the 2026-06-10 B3 rollout). Add dhcp-server (when active) to the package-update restart, like the etcd-endpoint-change handler already does.
- `/api/allocate` hardcodes `"existing": true` in its response, so callers can't tell a lookup from a fresh allocation — on 2026-06-10 this disguised an accidental allocation as a read during canary testing and fired a node-down alert. Return the real created/existing state (get_or_create_allocation knows it) and consider a `dry_run`/lookup-only query param for diagnostics.
- Go: health `Probe()` goroutines use bare `context.Background()` with no timeout (`health.go`); 4xx bodies only partially drained on retry (`handler.go:329`); ACL is allow-by-default for unlisted models (consider a deny-unknown mode).
- Python: global etcd client cached forever (`etcd_utils.py:86`), stale after member changes; mixed `print(file=sys.stderr)` vs logging in app.py; MAC normalization duplicated across ~5 files; nginx `-t` stderr discarded in `certbot_manager.py:215`.
- `/api/allocations` and `/api/health` expose full topology/health detail unauthenticated on the cluster network (recon value; acceptable under the LAN trust model once A1 is stated).

### Security
- etcd access hardening — **DONE** (mTLS-only / `enforce` on all core nodes; cert-possession is the boundary, no plaintext listeners; see `docs/design/etcd-access-hardening.md`). Remaining optional follow-up: **unify the cluster CA off RBD.** `setup-etcd-tls.yml` mints a dedicated etcd CA into the replicated `bootstrap_files_dir` + `/etc/ycluster/ca`, while `ca_manager.py` keeps a *separate* CA on RBD (`/rbd/misc/ca`, gated on `is_storage_leader`). Relocate `ca_manager`'s CA off RBD to the replicated filesystem path so one cluster CA serves etcd + blackbox + future mTLS: import the existing RBD CA to preserve already-issued blackbox certs, and relax the storage-leader gate to "CA key present". Not a blocker — etcd has its own working CA today.
- Clean up stale/vendor-created user accounts on managed nodes. In particular `vendoracct` on nv2 (Vendor factory-preinstall account, audited 2026-06-09): full sudo (`(ALL : ALL) ALL`), in the `lxd` group (root-equivalent), and a lingering login session (two bash shells + pipewire user services running since ~2026-05-22). Interim mitigation applied 2026-06-09: password locked (`passwd -l`) and `authorized_keys` renamed to `.disabled` (it held the cluster `ansible@pxe-server` key); SSH login verified refused. Note this was done ad-hoc on nv2 — a fresh vendor image would come up unmitigated. It also squats UID 1000, which is why `dev` is UID 1002 on nv2 instead of the cluster-standard 1000. Cleanup: verify nothing references `vendoracct` (cron, services, file ownership under /home/vendoracct), kill its sessions, `deluser --remove-home`; decide whether to renumber `dev` to UID 1000 (chown sweep) or leave it. Then sweep the other non-PXE node types (nvidia, nas, macos) for similar vendor/installer accounts and fold the check into their bootstrap playbooks.
- SSH-key rotation procedure (Ansible key at `/data/ansible_ssh_key` and any node-to-node keys).
- Secrets-rotation flow for things stored in etcd (`/cluster/config/inference/master-key` and similar) and in vault.
- Audit-log story: journald aggregation, etcd/Ceph audit logs, retention.
