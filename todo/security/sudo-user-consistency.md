# Sudo-capable users: consistent across the cluster

Make the set of sudo-capable users consistent across the cluster. Today
the privileged-account set drifts node-to-node — vendor/installer
accounts with full sudo on some nodes (see the vendor-account item
above), `dev` at a non-standard UID where a vendor squatted 1000, plus
assorted operator/service accounts added ad-hoc — so "who can sudo here"
depends on the box's provenance rather than a single declared policy.

## Plan

Define the intended privileged set once (which human/service accounts
get sudo, via which group, with what NOPASSWD scope) and enforce it from
Ansible across all managed node types (storage, compute, nvidia, nas,
macos), so unmanaged additions are flagged/removed and the same
accounts exist everywhere.

## Audit first

`getent group sudo`/`wheel`, `/etc/sudoers.d/*`, `lxd`/`incus-admin`
membership as root-equivalent — enumerate the drift before codifying.

Pairs with the vendor-account cleanup and SSH-key rotation items.
