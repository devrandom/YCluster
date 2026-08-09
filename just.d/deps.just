# Dependency hygiene for the Go inference proxy (local-ai-proxy).
#
# The audit and the pinning are deliberately separate flows:
#
#   just vulncheck    reachability-aware CVE scan (read-only, no pin writes)
#   just deps-verify  confirm the module cache still matches the pinned go.sum
#   just check        read-only audit gate = deps-verify + vulncheck
#   just deps-tidy    re-pin go.mod/go.sum to the code's real imports
#
# Why separate? `go mod tidy` / go.sum changes alter what a future scan
# covers, so pin updates belong in their own reviewed change, never as a
# side effect of a routine audit. Audit first (read-only); then when you
# do bump: `just deps-tidy` -> `just deps-verify` -> `just vulncheck`, and
# commit local-ai-proxy/go.mod + local-ai-proxy/go.sum together. go.sum is
# tracked so the checksummed set that was scanned is the set that ships.
#
# `just check` combines the two read-only audits (deps-verify + vulncheck).
# It never touches go.mod/go.sum, so running it routinely is safe; the
# mutation-prone deps-tidy stays a separate, deliberate step.

# Aggregate read-only audit gate: verify the pinned checksums, then run the
# reachability-aware CVE scan. Never rewrites go.mod/go.sum.
check: deps-verify vulncheck
    @echo "ok: checksums verified and no reachable vulnerabilities"

# Reachability-aware scan of local-ai-proxy deps against the OSV Go vuln DB.
# Static and non-executing; it does need network (or a warm vuln DB cache).
vulncheck:
    @command -v govulncheck >/dev/null 2>&1 || { echo "govulncheck not installed — run: go install golang.org/x/vuln/cmd/govulncheck@latest" >&2; exit 1; }
    @mkdir -p ./tmp/go-cache
    @GOCACHE="$(pwd)/tmp/go-cache" bash -c 'cd local-ai-proxy && govulncheck ./...'

# Verify the local module cache still matches the pinned go.sum (integrity
# check), and that go.sum is tracked so "what you scanned is what you ship".
deps-verify:
    @mkdir -p ./tmp/go-cache
    @GOCACHE="$(pwd)/tmp/go-cache" bash -c 'cd local-ai-proxy && go mod verify'
    @git ls-files --error-unmatch local-ai-proxy/go.sum >/dev/null 2>&1 || { echo "go.sum is not tracked — commit it so the scanned checksums ship" >&2; exit 1; }

# Re-pin go.mod/go.sum to the code's real imports, then verify integrity.
# Run after adding/removing an import or bumping a dependency. This is the
# maintenance flow that must land as its own commit (go.mod + go.sum).
deps-tidy:
    @mkdir -p ./tmp/go-cache
    @GOCACHE="$(pwd)/tmp/go-cache" bash -c 'cd local-ai-proxy && go mod tidy && go mod verify'
