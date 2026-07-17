# /api/allocate returns wrong "existing" flag

`/api/allocate` hardcodes `"existing": true` in its response, so
callers can't tell a lookup from a fresh allocation — this once
disguised an accidental allocation as a read during canary testing and
fired a node-down alert (incident:
`../ycluster-private/incidents/2026-06-10 — accidental c4 allocation
during canary testing`).

## Fix

Return the real created/existing state (`get_or_create_allocation`
knows it) and consider a `dry_run`/lookup-only query param for
diagnostics.
