# A1 — bootstrap trust model

From `docs/reviews/2026-06-09-codebase-review.md`. Bootstrap is
trust-on-first-use keyed to MAC addresses: a rogue LAN device can
PXE-join as any node type; `/bootstrap/*` is served unsigned to
`sudo bash`.

## Plan

- Document the LAN/TOFU trust model in `ARCHITECTURE.md`.
- Cheap hardening: MAC-OUI validation + checksum-verified bootstrap
  scripts.
