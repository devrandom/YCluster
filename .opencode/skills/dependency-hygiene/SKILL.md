---
name: dependency-hygiene
description: Audit and re-pin the Go module in local-ai-proxy. Run reachability-aware govulncheck audits and manage go.mod/go.sum pins — keeping the read-only audit and the write-prone re-pinning as separate flows.
---

# Dependency hygiene (local-ai-proxy Go module)

`local-ai-proxy` is a Go module checked out under the ycluster repo, with
`just` recipes in `just.d/deps.just`. Two concerns that must stay separate:

- **Audit** — "are the pinned deps clean?" This is read-only and never
  rewrites `go.mod`/`go.sum`.
- **Pin maintenance** — "re-pin to the code's real imports / a newer
  version." This writes `go.mod`/`go.sum` and therefore changes what a
  future audit covers. It belongs in its own reviewed commit.

Never let a routine audit silently mutate the pins, and never fold a pin
change into an unrelated commit.

## Audit (read-only)

```bash
just vulncheck        # govulncheck ./... — reachability-aware scan vs OSV Go vuln DB
just deps-verify      # go mod verify + confirms go.sum is git-tracked
```

- `just vulncheck` is static and non-executing; it needs network (or a
  warm vuln DB cache) and `govulncheck` installed. Install once with
  `just deps-tools` (which also sets up `osv-scanner` and `pip-audit`;
  or directly `go install golang.org/x/vuln/cmd/govulncheck@latest`). It exits
  **non-zero (3) when reachable vulnerabilities are found** — treat that
  as a hard fail, not a warning.
- `just deps-verify` fails if the local module cache no longer matches
  `go.sum` (tampering) or if `go.sum` isn't tracked — so the checksummed
  set that was scanned is the set that ships.

Use these as the gate before shipping. Run them on their own; do not
couple them to `go mod tidy`.

## Pin maintenance (write-prone, separate commit)

When an import is added/removed or a dependency is bumped:

```bash
just deps-tidy        # go mod tidy && go mod verify — re-pins go.mod/go.sum
just deps-verify      # confirm cache matches the new go.sum
just vulncheck        # re-audit the newly pinned set
```

Then commit `local-ai-proxy/go.mod` and `local-ai-proxy/go.sum` together
as one reviewed change. `go.sum` must stay tracked.

## Reading a govulncheck result

- Reachable findings (listed with example traces) are the ones that matter
  and must be fixed or consciously accepted.
- Findings in imported packages or required modules with no trace mean the
  path isn't reached — note them but don't treat them as blockers.
- Stdlib findings are fixed by a Go toolchain bump; module findings are
  fixed by `go get` + `deps-tidy`.
