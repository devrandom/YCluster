# TODO

One file per issue, grouped by subject matter. Items within each section
are ordered by priority — top is "do this next". Items flagged **[HIGH]**
caused real outages, are cross-cutting and unblock other items, or are
otherwise urgent. Completed or no-longer-relevant items move to
`archive/` and are listed in `DONE.md`.

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
- **[HIGH]** [etcd RBAC by client cert](security/etcd-rbac-by-cert.md) — one shared `etcd-client` cert today; an Incus-host compromise leaks every secret in etcd. Split identity into `etcd-storage` (all s*) and `etcd-compute` (c*/nv*/nas*), turn on RBAC, consolidate secret prefixes under `/cluster/config/`.
- **[MEDIUM]** [Sudo-user consistency](security/sudo-user-consistency.md) — paired with vendor cleanup; privileged set drifts node-to-node today.
- [Eval: should `dev` be a macOS admin?](security/dev-macos-admin-eval.md) — `setup-macos.yml` gives `dev` the `admin` primary group, unlike Linux where dev is deliberately not a sudoer.
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
- [Single source of truth for public vhost configs](networking/single-source-vhosts.md) — per-app vhost templates (rathole `:443`) and the frp aggregator (`127.0.0.2:17443`) drift apart; new vhosts silently 404 on the live public path (comfy).
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
- **[MEDIUM]** [Container image scan-drift](monitoring/container-image-scan-drift.md) — snapshot vs running image can diverge (authentik false positives from a drifted snapshot); `PerimeterCVE*` alerts still surface it for now, so medium.
- [Postgres + Qdrant liveness](monitoring/pg-qdrant-liveness.md)
- [Backup freshness metric](monitoring/backup-freshness-metric.md)
- [Clock-skew alert](monitoring/clock-skew-alert.md)
- [Ansible-run-success metric](monitoring/ansible-run-success-metric.md)
- [Follow-on: failed-systemd-units](monitoring/follow-on-failed-systemd-units.md)
- [Move NTFY_TOPIC out of repo](monitoring/ntfy-topic-out-of-repo.md) — production UUID hardcoded in ntfy-webhook.py; read from env/vault instead.

## Operations
- **[HIGH]** [Storage-leader election: graceful teardown](operations/storage-leader-graceful-teardown.md) — every routine failover today takes the aggressive path; postgres crash-stops on every drain.
- **[HIGH]** [admin-api etcd client failover](operations/admin-api-etcd-failover.md) — cached client pinned to first reachable host; on drain the health surface goes blank with a fresh timestamp, looking like a stuck failover.
- [Clean up searxng-docker stack](operations/searxng-cleanup.md) — stack removed 2026-08-01; remaining sub-items are certbot warts + container-state alerting.
- **[MEDIUM]** [Move storage-node services from docker to podman](operations/move-docker-to-podman.md) — grafana, buildkitd, open-webui, authentik, docker-registry, valkey still on docker; podman is already standard elsewhere.
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
- [DS4 infernal: watch for DCP support, then TP2+DCP2 vs TP4](inference/ds4-infernal-dcp.md) — II launcher gates `DCP_SIZE=1` for DS4 DSpark and has no PP axis, leaving TP4 as the only 4-GPU 256K shape; re-test TP2+DCP2 once a build lifts the gate.
- [vllm-mlx on m1 ops gaps](inference/vllm-mlx-ops.md)
- [Upstream exo prompt-logging fix](inference/upstream-exo-prompt-logging.md)
- [GPU-VM DKMS → Canonical-signed modules](inference/gpu-dkms-canonical.md)
- [MiniMax-M3 max-num-seqs knob](inference/m3-max-num-seqs.md)
- [CDI spec regeneration on GPU host](inference/cdi-spec-regen.md) — stale `/etc/cdi/nvidia.yaml` after driver upgrade or vfio→nvidia rebind; podman CDI fails on ghost devices. Wire into `gpu-passthrough-bind.sh.j2`.

## Authentik
- [Split passwords frontend from general](authentik/split-passwords-frontend.md)
- [Drop bcrypt hasher](authentik/drop-bcrypt-hasher.md)
