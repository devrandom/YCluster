# TODO

- Document MiniMax M2.5 client-side workarounds in `docs/operations/inference.md`: (a) use `/v1/completions` (not `/v1/chat/completions`) with a raw text prompt ending in the desired prefix for shaped output — the chat-completions `continue_final_message` flag is silently ignored by vllm-mlx 0.2.7; (b) client-side XML parsing of `<minimax:tool_call>` envelopes; (c) client-side truncation to emulate `stop`; (d) strip trailing `[e~[` from `.content` / `.text`. See `research/minimax-vllm-mlx-structured-output-2026-04-11.md`.
- vllm-mlx on m1 has a stack of upstream bugs and ad-hoc operational gaps (no `stop`, no guided decoding, broken special-token detokenization, no MiniMax tool-call parser, no pinned version, launch commands unscripted) — parked in `research/minimax-vllm-mlx-structured-output-2026-04-11.md` under "Upstream work queue" and "Operational gaps (m1)". Not actionable in the near term.
- Authenticate the "drain" function on the cluster status page
- Rebuild rathole with PROXY protocol support so the public admin-api (wg bootstrap + /status) sees real client IPs. Current rathole 0.5.0 is a plain L4 TCP tunnel — everything forwarded from the public host lands on 127.0.0.2 with source 127.0.0.1, so per-client rate limits and audit logs are impossible. nginx side would then use `listen 127.0.0.2:443 ssl proxy_protocol; set_real_ip_from 127.0.0.1; real_ip_header proxy_protocol;` once rathole emits the PP header. Upstream rathole doesn't support it — may need a fork/patch.
- Upstream the exo prompt-logging fix. exo leaks prompt content into its logs (and the on-disk log file) at INFO — `logger.info(prompt)` in `utils_mlx.py` plus `InputMessageContent.__repr__` showing the first 100 chars, which every task/command-object log dump recurses into. We carry a 2-hunk patch in `config/ansible/macos/files/exo/patches/0001-no-prompt-logging.patch`; submit it as a PR to exo-explore/exo so the carried patch can eventually be dropped.
- Harden the gateway uplink health check (`/usr/local/lib/cluster/check-gateway-health`). It currently fails the gateway VIP on a single HTTP GET to one external host (`detectportal.firefox.com`), so any brief ISP/DNS blip flaps the gateway VIP — and on 2026-06-01 21:41 a ~3-min upstream outage did exactly that, cascading into a storage-leadership failover and a cluster-wide WireGuard flap (nv1/nv2/nv3/c3 dropped). Improvements to investigate: (1) probe multiple targets and only fail if all are unreachable; (2) raw-IP ICMP/TCP fallback to the next hop / a known anycast IP so a DNS hiccup alone can't trip it; (3) require N consecutive failures (raise `fall`) and/or lengthen `interval` to ride out sub-minute stutters; (4) distinguish "uplink NIC/link down" (fail fast) from "internet unreachable" (tolerate longer) since they have different blast radii. Goal: a transient WAN stutter should not move the gateway VIP at all.
- WG inbound ingress needs a stable externally-routable endpoint that follows the active leader. Currently the DNAT on the upstream router points at a specific core node's uplink IP (e.g. `192.168.0.104` = s3), so wg breaks on gateway-VIP failover to s1/s2. Options to investigate: (1) uplink-side VIP via keepalived on the 192.168.0.x segment — clean but only s2/s3 are on that subnet (s1 is on 192.168.9.x), so s1 can't participate and gateway-VIP leadership would need to be constrained to s2/s3; (2) static route `10.0.0.0/24 via <core node>` on the router so DNAT can target `10.0.0.254` directly — simplest but single-next-hop fragility; (3) a tiny UDP forwarder on each core node that knows which peer currently holds the gateway VIP and proxies wg packets there. Option 1 is probably the right call once s1's uplink is reconciled.

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

### Security
- SSH-key rotation procedure (Ansible key at `/data/ansible_ssh_key` and any node-to-node keys).
- Secrets-rotation flow for things stored in etcd (`/cluster/config/inference/master-key` and similar) and in vault.
- Audit-log story: journald aggregation, etcd/Ceph audit logs, retention.
