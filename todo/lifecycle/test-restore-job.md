# Periodic test-restore job

Decrypt the latest backup to a scratch location and verify pg/qdrant/etcd
load cleanly. Alert on failure or stale (>N day) backups. Two-layer
approach to avoid widening attack surface.

## Layer 1 — Nightly automated, on the storage leader

Keep an age privkey on the storage leader (root-only, same FS as live
pg/etcd data — putting it there doesn't expand blast radius since the
host already holds plaintext) and restore the latest backup into a
scratch pg cluster on a non-default port + a temp qdrant + an etcd
snapshot dir. Smoke-query, then wipe.

**Caveat:** enforce strict mode/ownership on the privkey file in the
playbook (root:root 0400, on the same encrypted FS as the DB) — its
"no extra surface" property depends on that.

## Layer 2 — Quarterly attended drill with a hardware key

Use `age-plugin-yubikey` (or TPM-backed) so the privkey never lives on
disk anywhere. Operator plugs the token into a clean drill host,
decrypts the latest offsite backup, restores end-to-end, runs smoke
checks. This is what actually proves we can recover from
compromise/loss of the storage leader.

Token lives in a safe between drills; document key custody and a
reissuance procedure for when an operator leaves.
