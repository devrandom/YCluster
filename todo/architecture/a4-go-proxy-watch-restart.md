# A4 — Go proxy etcd-watch not restarted on break

From `docs/reviews/2026-06-09-codebase-review.md`.
`source.go:134` doesn't restart the etcd watch on break, so model config
silently stops hot-reloading; `disabled.go:32` shows the
disabled-backends set goes stale on etcd outage.

## Fix

Add watch-error → re-list-and-rewatch.
