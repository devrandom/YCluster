# Go: health + handler hygiene

From `docs/reviews/2026-06-09-codebase-review.md` Minor section.

- **Health `Probe()` goroutines** use bare `context.Background()` with no
  timeout (`health.go`).
- **4xx bodies only partially drained** on retry (`handler.go:329`).
- **ACL is allow-by-default** for unlisted models — consider a
  deny-unknown mode.
