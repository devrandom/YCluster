# H2 — ignore_errors hides hung services before destructive steps

From `docs/reviews/2026-06-09-codebase-review.md`. `ignore_errors: yes`
on stop/teardown paths (`wipe-etcd.yml`,
`storage/stop-storage-leader-election.yml`) hides hung services before
destructive steps. Use `failed_when:` with explicit checks.
