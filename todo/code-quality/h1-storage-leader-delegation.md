# H1 — storage-leader delegation breaks under --limit

From `docs/reviews/2026-06-09-codebase-review.md`. `groups['storage'][0]`
delegation (~20× in `storage/setup-user-rbd.yml`) breaks under
`--limit`. Use the mountpoint-based leader-detection convention
(see `docs/patterns/storage-leader.md`).
