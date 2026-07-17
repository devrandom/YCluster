# A3 — RBD setup-time maps lack `--exclusive`

From `docs/reviews/2026-06-09-codebase-review.md`.
`storage/setup-user-rbd.yml:68` and `storage/setup-misc-rbd.yml:29`
don't pass `--exclusive` at setup time, though runtime mounts do. Add
for consistency.
