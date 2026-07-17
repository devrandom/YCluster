# TODO

One file per issue, grouped by subject matter. Items within each section
are ordered by priority — top is "do this next". Items flagged **[HIGH]**
caused real outages, are cross-cutting and unblock other items, or are
otherwise urgent. Completed or no-longer-relevant items move to
`archive/`.

Categories: [security](#security) ·
[networking](#networking) ·
[lifecycle](#lifecycle) ·
[incus](#incus) ·
[monitoring](#monitoring) ·
[operations](#operations) ·
[architecture](#architecture) ·
[code-quality](#code-quality) ·
[inference](#inference) ·
[authentik](#authentik)

## Security
- **[HIGH]** [Vendor account cleanup](security/vendor-account-cleanup.md) — vendor preinstalls ship with full sudo + lxd membership + populated authorized_keys; fresh vendor image comes up unmitigated.
- **[MEDIUM]** [Sudo-user consistency](security/sudo-user-consistency.md) — paired with vendor cleanup; privileged set drifts node-to-node today.
- **[MEDIUM]** [SSH-key rotation](security/ssh-key-rotation.md) — no procedure; full cluster compromise if the Ansible key leaks.
- **[MEDIUM]** [Secrets-rotation](security/secrets-rotation.md) — paired; etcd `master-key` and vault secrets have no rotation flow.
- [Audit-log story](security/audit-log-story.md)
- [Reconsider become usage](security/become-model-audit.md)
- [core group definition](security/core-group-definition.md)

## Networking
- **[HIGH]** [Switch rathole → frp](networking/switch-rathole-to-frp.md) — rathole upstream dormant + TCP socket leak; real external HTTPS outage 2026-07-17.
- **[HIGH]** [WG inbound ingress follows the active leader](networking/wg-inbound-follow-leader.md) — DNAT on a single uplink IP breaks remote access on gateway-VIP failover.
- **[HIGH]** [Per-node uplink failover watchdog](networking/uplink-failover-watchdog.md) — dead uplink behind a live router blackholes egress (rathole breaks too) while the metric-100 DHCP route stays.
- **[MEDIUM]** [ycluster wg delete: peer-only variant](networking/wg-delete-cascade.md) — `wg delete` silently drops live allocations; workaround documented (`wg revoke`) but not enforced.
- [ycluster https set-domain blast-radius checklist](networking/ycluster-https-set-domain-checklist.md)

## Lifecycle
DR, backups, node lifecycle, scaling/capacity.

### Backups & DR
- **[HIGH]** [DR runbooks](lifecycle/dr-runbooks.md) — etcd quorum loss is the only unrecoverable failure mode; `etcd.md`/`ceph.md` point at procedures that don't exist.
- **[HIGH]** [Ceph off-cluster backup](lifecycle/ceph-off-cluster-backup.md) — paired with DR; docker registry blobs + any hand-placed RBD content has no off-cluster copy.
- [Periodic test-restore job](lifecycle/test-restore-job.md)
- [Backup restore subcommand](lifecycle/backup-restore-subcommand.md)
- [Age private-key custody doc](lifecycle/age-key-custody-doc.md)

### Node lifecycle
- [Replace a dead node runbook](lifecycle/replace-dead-node-runbook.md)
- [Graceful decommission runbook](lifecycle/graceful-decommission-runbook.md)
- [ycluster node add/remove/drain](lifecycle/ycluster-node-add-remove-drain.md)
- [ycluster failover](lifecycle/ycluster-failover.md)

### Scaling & capacity
- [Add-storage-node](lifecycle/add-storage-node.md)
- [Add-compute-node](lifecycle/add-compute-node.md)
- [Ceph capacity-planning](lifecycle/ceph-capacity-planning.md)

## Incus
- **[HIGH]** [Incus storage dir→zfs](incus/incus-storage-dir-to-zfs.md) — runaway-guest root-fs fill; caused the nv3 GPU/driver-loss cascade.
- [Build incus VM images once, copy between hosts](incus/incus-vm-images-build-once.md)
- [Dev-cluster system tests — incus DNS](incus/dev-cluster-system-tests-dns.md)
- [Dev cluster: browsable external/TLS path](incus/dev-cluster-browsable-tls.md)
- [VM scheduling iter 3: web-driven creation](incus/vm-scheduling-iter3.md)
- [ycluster vm set-gpus](incus/ycluster-vm-set-gpus.md)

## Monitoring
- **[MEDIUM]** [GPU health](monitoring/gpu-health-monitoring.md) — would have caught the nv3 GPU/driver-loss cascade (temperature / ECC / FLR events).
- **[MEDIUM]** [VM guest disk-full](monitoring/vm-disk-full-alerts.md) — same blind spot; root cause of the same nv3 cascade.
- [Postgres + Qdrant liveness](monitoring/pg-qdrant-liveness.md)
- [Backup freshness metric](monitoring/backup-freshness-metric.md)
- [Clock-skew alert](monitoring/clock-skew-alert.md)
- [Ansible-run-success metric](monitoring/ansible-run-success-metric.md)
- [Follow-on: failed-systemd-units](monitoring/follow-on-failed-systemd-units.md)

## Operations
- **[HIGH]** [Storage-leader election: graceful teardown](operations/storage-leader-graceful-teardown.md) — every routine failover today takes the aggressive path; postgres crash-stops on every drain.
- **[HIGH]** [admin-api etcd client failover](operations/admin-api-etcd-failover.md) — cached client pinned to first reachable host; on drain the health surface goes blank with a fresh timestamp, looking like a stuck failover.
- **[HIGH]** [Clean up searxng-docker stack](operations/searxng-cleanup.md) — caused the 2026-06-11 tunnel outage; one `compose up` resurrects caddy (the dir is still on disk).
- [Consolidate redundant etcd reads](operations/consolidate-etcd-reads.md)
- [s4: validate as storage-leader](operations/s4-leader-validation.md)
- [Periodic Docker image pruning](operations/docker-image-pruning.md)
- [install-mineru rebuild detection](operations/install-mineru-rebuild-detection.md)
- [NOTES.md known issues → tickets](operations/notes-known-issues.md)
- [ansible-lint CI](operations/ansible-lint-ci.md)
- [ansible-playbook --check](operations/ansible-playbook-check.md)
- [Pre-commit yaml/jinja lint](operations/pre-commit-yaml-jinja.md)

## Architecture
- **[MEDIUM]** [A1 bootstrap trust model](architecture/a1-bootstrap-trust-model.md) — TOFU on MAC + unsigned `/bootstrap/*`; rogue LAN device can PXE-join as any node type. Document + cheap hardening (MAC-OUI, checksum).
- **[MEDIUM]** [A4 Go proxy etcd-watch restart](architecture/a4-go-proxy-watch-restart.md) — model config silently stops hot-reloading on etcd-watch break; affected every user-facing inference path.
- [A2 API keys plaintext](architecture/a2-api-keys-plaintext.md)
- [A3 RBD setup-time --exclusive](architecture/a3-rbd-exclusive-flag.md)

## Code quality
- **[HIGH]** [cluster-heartbeat error conditions](code-quality/cluster-heartbeat-error-conditions.md) — observed 2026-07-09: heartbeat logged HEALTHY + Last Ping: Success while underneath Connection refused. Monitoring blind spot that masks outages.
- **[MEDIUM]** [/api/allocate existing flag](code-quality/api-allocate-existing-flag.md) — masked allocation as read during canary testing; fired a false node-down alert.
- [H1 storage-leader delegation](code-quality/h1-storage-leader-delegation.md) — breaks under `--limit`
- [H2 ignore_errors destructive](code-quality/h2-ignore-errors-destructive.md)
- [H3 systemd hardening](code-quality/h3-systemd-hardening.md)
- [H4 docker-registry UID](code-quality/h4-docker-registry-uid.md)
- [dhcp-server restart on package update](code-quality/dhcp-server-restart-on-package-update.md)
- [Go: health + handler hygiene](code-quality/go-health-probe-context.md)
- [Python: global etcd client cached](code-quality/python-etcd-cached-client.md)
- [ycluster CLI etcd-client.env autoload](code-quality/ycluster-cli-etcd-hosts-autoload.md)
- [/api/allocations + /api/health unauth](code-quality/api-allocations-health-unauth.md)

## Inference
- **[MEDIUM]** [Reproducible build of b12x SM120 inference image](inference/b12x-reproducible-build.md) — supply chain; running an unaudited third-party image on a GPU node long-term.
- [MiniMax-M3 reasoning parser](inference/m3-reasoning-parser.md)
- [vllm-mlx on m1 ops gaps](inference/vllm-mlx-ops.md)
- [Upstream exo prompt-logging fix](inference/upstream-exo-prompt-logging.md)
- [GPU-VM DKMS → Canonical-signed modules](inference/gpu-dkms-canonical.md)
- [MiniMax-M3 max-num-seqs knob](inference/m3-max-num-seqs.md)

## Authentik
- [Split passwords frontend from general](authentik/split-passwords-frontend.md)
- [Drop bcrypt hasher](authentik/drop-bcrypt-hasher.md)

## Archive
Completed or no-longer-relevant items. Re-open by moving back to a
subject-matter folder.

- [Sandbox MinerU's outbound](archive/sandbox-mineru-outbound.md) — Linux/Nvidia done (`e5ecb1b` / `0f8729a`); macOS deferred, `setup-mineru.yml` now hard-aborts until the Seatbelt profile lands.
