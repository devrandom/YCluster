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
- **[HIGH]** [Sandbox MinerU's outbound](security/sandbox-mineru-outbound.md) (Linux/Nvidia done, macOS Seatbelt TBD)
- [Vendor account cleanup](security/vendor-account-cleanup.md)
- [Sudo-user consistency](security/sudo-user-consistency.md)
- [SSH-key rotation](security/ssh-key-rotation.md)
- [Secrets-rotation](security/secrets-rotation.md)
- [Audit-log story](security/audit-log-story.md)
- [Reconsider become usage](security/become-model-audit.md)
- [core group definition](security/core-group-definition.md)

## Networking
- **[HIGH]** [Switch rathole → frp](networking/switch-rathole-to-frp.md) — rathole upstream dormant + TCP socket leak; real external HTTPS outage 2026-07-17
- [WG inbound ingress follows the active leader](networking/wg-inbound-follow-leader.md) — DNAT on a single uplink IP breaks on gateway failover
- [Per-node uplink failover watchdog](networking/uplink-failover-watchdog.md)
- [ycluster wg delete: peer-only variant](networking/wg-delete-cascade.md)
- [ycluster https set-domain blast-radius checklist](networking/ycluster-https-set-domain-checklist.md)

## Lifecycle
DR, backups, node lifecycle, scaling/capacity.

### Backups & DR
- [DR runbooks](lifecycle/dr-runbooks.md) — etcd quorum loss is the only unrecoverable failure mode; Ceph corruption, cold-start, VIP split-brain
- [Ceph off-cluster backup](lifecycle/ceph-off-cluster-backup.md)
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
- [Incus storage dir→zfs](incus/incus-storage-dir-to-zfs.md) — runaway-guest root-fs fill (nv3 cascade)
- [Build incus VM images once, copy between hosts](incus/incus-vm-images-build-once.md)
- [Dev-cluster system tests — incus DNS](incus/dev-cluster-system-tests-dns.md)
- [Dev cluster: browsable external/TLS path](incus/dev-cluster-browsable-tls.md)
- [VM scheduling iter 3: web-driven creation](incus/vm-scheduling-iter3.md)
- [ycluster vm set-gpus](incus/ycluster-vm-set-gpus.md)

## Monitoring
- [GPU health](monitoring/gpu-health-monitoring.md) — nv3 GPU/driver-loss cascade wouldn't have been caught
- [VM guest disk-full](monitoring/vm-disk-full-alerts.md)
- [Postgres + Qdrant liveness](monitoring/pg-qdrant-liveness.md)
- [Backup freshness metric](monitoring/backup-freshness-metric.md)
- [Clock-skew alert](monitoring/clock-skew-alert.md)
- [Ansible-run-success metric](monitoring/ansible-run-success-metric.md)
- [Follow-on: failed-systemd-units](monitoring/follow-on-failed-systemd-units.md)

## Operations
- [admin-api etcd client failover](operations/admin-api-etcd-failover.md)
- [Consolidate redundant etcd reads](operations/consolidate-etcd-reads.md)
- [Storage-leader election: graceful teardown](operations/storage-leader-graceful-teardown.md)
- [s4: validate as storage-leader](operations/s4-leader-validation.md)
- [Periodic Docker image pruning](operations/docker-image-pruning.md)
- [install-mineru rebuild detection](operations/install-mineru-rebuild-detection.md)
- [Clean up searxng-docker stack](operations/searxng-cleanup.md)
- [NOTES.md known issues → tickets](operations/notes-known-issues.md)
- [ansible-lint CI](operations/ansible-lint-ci.md)
- [ansible-playbook --check](operations/ansible-playbook-check.md)
- [Pre-commit yaml/jinja lint](operations/pre-commit-yaml-jinja.md)

## Architecture
- [A1 bootstrap trust model](architecture/a1-bootstrap-trust-model.md) — rogue LAN device can PXE-join as any node type
- [A2 API keys plaintext](architecture/a2-api-keys-plaintext.md)
- [A3 RBD setup-time --exclusive](architecture/a3-rbd-exclusive-flag.md)
- [A4 Go proxy etcd-watch restart](architecture/a4-go-proxy-watch-restart.md)

## Code quality
- [H1 storage-leader delegation](code-quality/h1-storage-leader-delegation.md) — breaks under `--limit`
- [H2 ignore_errors destructive](code-quality/h2-ignore-errors-destructive.md)
- [H3 systemd hardening](code-quality/h3-systemd-hardening.md)
- [H4 docker-registry UID](code-quality/h4-docker-registry-uid.md)
- [dhcp-server restart on package update](code-quality/dhcp-server-restart-on-package-update.md)
- [/api/allocate existing flag](code-quality/api-allocate-existing-flag.md)
- [Go: health + handler hygiene](code-quality/go-health-probe-context.md)
- [Python: global etcd client cached](code-quality/python-etcd-cached-client.md)
- [cluster-heartbeat error conditions](code-quality/cluster-heartbeat-error-conditions.md)
- [ycluster CLI etcd-client.env autoload](code-quality/ycluster-cli-etcd-hosts-autoload.md)
- [/api/allocations + /api/health unauth](code-quality/api-allocations-health-unauth.md)

## Inference
- [Reproducible build of b12x SM120 inference image](inference/b12x-reproducible-build.md) — supply-chain; opaque prebuilt image
- [MiniMax-M3 reasoning parser](inference/m3-reasoning-parser.md)
- [vllm-mlx on m1 ops gaps](inference/vllm-mlx-ops.md)
- [Upstream exo prompt-logging fix](inference/upstream-exo-prompt-logging.md)
- [GPU-VM DKMS → Canonical-signed modules](inference/gpu-dkms-canonical.md)
- [MiniMax-M3 max-num-seqs knob](inference/m3-max-num-seqs.md)

## Authentik
- [Split passwords frontend from general](authentik/split-passwords-frontend.md)
- [Drop bcrypt hasher](authentik/drop-bcrypt-hasher.md)

## Archive
Completed or no-longer-relevant items. Currently empty.
