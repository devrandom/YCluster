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
    @command -v govulncheck >/dev/null 2>&1 || { echo "govulncheck not installed — run: just deps-tools" >&2; exit 1; }
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

# Install (or refresh) the dependency-hygiene audit tooling the gate needs —
# encodes what were one-off installs so a fresh machine can stand it up:
#
#   govulncheck  (S1)      go install golang.org/x/vuln/cmd/govulncheck
#   osv-scanner  (S2/S4/S6/S7/S9/S10)  go install google/osv-scanner
#   pip-audit    (S3)      uv tool install
#
# The Go binaries are built from source (`go install`) per the repo's
# no-prebuilt-binaries rule and land in "$(go env GOPATH)/bin"; pip-audit
# lands as a uv tool. Where those land depends on env: under a Codex session
# .codex/env redirects GOPATH and the uv tool dirs into tmp/ (writable even
# when ~/go and ~/.local are read-only); on a normal dev machine they use the
# user defaults (~/go/bin, ~/.local/bin). Keep the tools' bin dir on PATH.
# Re-running refreshes in place, so it's safe once per machine.
deps-tools:
    #!/usr/bin/env bash
    set -euo pipefail
    if ! command -v go >/dev/null 2>&1; then
        echo "go not found — install the Go toolchain first (see docs/CONTRIBUTING.md)" >&2
        exit 1
    fi
    go install golang.org/x/vuln/cmd/govulncheck@latest
    go install github.com/google/osv-scanner/v2/cmd/osv-scanner@latest
    if ! command -v uv >/dev/null 2>&1; then
        if [ -x "$HOME/.local/bin/uv" ]; then
            export PATH="$HOME/.local/bin:$PATH"
        else
            echo "uv not found; install with 'pip install uv'" >&2
            exit 1
        fi
    fi
    uv tool install pip-audit
    echo "ok: dep tooling installed — $(command -v govulncheck), $(command -v osv-scanner), $(command -v pip-audit)"

# ---------------------------------------------------------------------------
# Aggregated dependency hygiene across every shipped surface (docs/design/
# dependency-hygiene.md). Audit flows are read-only; re-pins are separate.
#
#   deps-check        one gate = deps-verify + vulncheck + deps-vuln-py +
#                                 deps-vuln-src
#   deps-vuln-py      S2 root uv.lock, S3 ycluster requirements, S4 mcp uv.lock
#   deps-vuln-src     S6 podman, S7 frp, S9 zulip, S10 open-webui (+plugins):
#                     osv on the pinned build-tree checkouts + pin drift check
#   deps-tidy-go      go mod tidy (S1)
#   deps-tidy-py      uv lock (S2, S4)
#   deps-tidy-src     emit a proposed new SHA/bump for the build-*.yml pins
#                     (never auto-writes — always lands as its own commit)
# ---------------------------------------------------------------------------

# The one gate that answers "is everything we run clean?" across surfaces.
# Read-only end to end. On surfaces still being baselined (Phase 2 of
# dependency-hygiene.md) the sub-recipes report but do not hard-fail yet;
# strict enforcement lands in Phase 4 once the accepted-findings ledger is set.
deps-check: deps-verify vulncheck deps-vuln-py deps-vuln-src
    @echo "ok: dependency audit complete (review findings above)"

# Install (or refresh) the local osv-feed CVE feed tool from ./osv-feed.
# Stdlib-only, so `uv tool install` pulls no runtime dependencies. Same
# command as uv tool install ./osv-feed.
osv-feed-tool:
    @uv tool install ./osv-feed

# Python surfaces S2/S3/S4: osv-scanner on each uv.lock, `uv lock --check`
# proving the lock still matches pyproject, and pip-audit on the ycluster CLI
# requirements (S3, which mirrors the apt runtime deps and has no lock).
# The osv-scanner/pip-audit steps are report-only for now: these surfaces are
# still being baselined (Phase 2) and findings are captured into the
# accepted-findings ledger before they gate. `uv lock --check` stays strict —
# it proves freshness, not vulnerability posture.
deps-vuln-py:
    @command -v osv-scanner >/dev/null 2>&1 || { echo "osv-scanner not installed — run: just deps-tools" >&2; exit 1; }
    @command -v pip-audit >/dev/null 2>&1 || { echo "pip-audit not installed — run: just deps-tools" >&2; exit 1; }
    @echo "== S2 root uv.lock: uv lock --check =="
    @uv lock --check
    @echo "== S2 root uv.lock: osv-scanner (report-only, Phase 2 baseline) =="
    @osv-scanner --lockfile uv.lock || true
    @echo "== S3 ycluster CLI requirements: pip-audit (report-only, Phase 2 baseline) =="
    @pip-audit -r config/ansible/admin/files/ycluster/requirements.txt || true
    @echo "== S4 mcp/cluster-exec uv.lock: uv lock --check =="
    @cd mcp/cluster-exec && uv lock --check
    @echo "== S4 mcp/cluster-exec uv.lock: osv-scanner (report-only, Phase 2 baseline) =="
    @osv-scanner --lockfile mcp/cluster-exec/uv.lock || true

# Pinned source-build surfaces (S6 podman, S7 frp, S9 zulip, S10 open-webui +
# plugins): osv-scanner over each pinned build tree's lockfiles, and a pin
# drift check. The build trees are gitignored scratch checkouts under ext/,
# pinned to a SHA in the build-*.yml playbooks; a fresh clone must
# reproduce them from those pins before this recipe can scan (Phase 2).
# Report-only for now: pinned vendored trees are baselined in Phase 2 and the
# final freshness policy is decided there. S8 (rathole) is removed — the
# rip-out landed.
deps-vuln-src:
    @command -v osv-scanner >/dev/null 2>&1 || { echo "osv-scanner not installed — run: just deps-tools" >&2; exit 1; }
    @for entry in \
        "podman ext/podman config/ansible/admin/build-podman.yml podman_repo_sha" \
        "frp ext/frp config/ansible/app/build-frp.yml frp_repo_sha" \
        "zulip ext/docker-zulip config/ansible/app/build-zulip.yml zulip_repo_sha" \
        "open-webui ext/open-webui config/ansible/app/build-open-webui.yml webui_repo_sha" \
        "open-webui-plugins ext/open-webui-plugins config/ansible/app/build-open-webui.yml plugins_repo_sha"; do \
        set -- $entry; surf="$1"; dir="$2"; pb="$3"; var="$4"; \
        if [ ! -d "$dir" ]; then \
            echo "== SRC $surf: no checkout at $dir — clone from the $pb pin first =="; continue; \
        fi; \
        pin=$(sed -n "s/^[[:space:]]*${var}:[[:space:]]*[\"']\([^\"']*\)[\"'].*/\1/p" "$pb" | head -n1); \
        if [[ "$pin" == *"://"* ]]; then \
            echo "== SRC $surf: repo-URL pin (not yet a SHA) — pin $var to a SHA per repo rule =="; \
        else \
            head_sha=$(git -C "$dir" rev-parse HEAD 2>/dev/null || true); \
            tag=$(git -C "$dir" describe --tags --exact-match HEAD 2>/dev/null || true); \
            if [ -n "$pin" ] && [ -n "$head_sha" ] && [ "$head_sha" != "$pin" ] && [ "$tag" != "$pin" ]; then \
                echo "== SRC $surf: DRIFT — checkout $head_sha / $tag vs pin $pin =="; \
            else \
                echo "== SRC $surf: checkout matches pin ($pin) =="; \
            fi; \
        fi; \
        echo "== SRC $surf: osv-scanner $dir =="; \
        osv-scanner "$dir" 2>&1 || true; \
    done

# Check out each pinned source-build tree (S6/S7/S9/S10) at the SHA pinned
# in its build-*.yml playbook, so `deps-vuln-src` scans the same code a build
# would use. Scaffolds ext/ from scratch if a checkout is missing. Idempotent;
# run once per machine after `deps-tools` (ext/ is gitignored scratch).
deps-src-checkout:
    @mkdir -p ./ext
    @for entry in \
        "podman https://github.com/containers/podman.git ext/podman config/ansible/admin/build-podman.yml podman_repo_sha" \
        "frp https://github.com/fatedier/frp.git ext/frp config/ansible/app/build-frp.yml frp_repo_sha" \
        "zulip https://github.com/zulip/docker-zulip.git ext/docker-zulip config/ansible/app/build-zulip.yml zulip_repo_sha" \
        "open-webui https://github.com/devrandom/open-webui.git ext/open-webui config/ansible/app/build-open-webui.yml webui_repo_sha" \
        "open-webui-plugins https://github.com/devrandom/open-webui-plugins.git ext/open-webui-plugins config/ansible/app/build-open-webui.yml plugins_repo_sha"; do \
        set -- $entry; surf="$1"; url="$2"; dir="$3"; pb="$4"; var="$5"; \
        pin=$(sed -n "s/^[[:space:]]*${var}:[[:space:]]*[\"']\([^\"']*\)[\"'].*/\1/p" "$pb" | head -n1); \
        if [ -z "$pin" ]; then echo "SKIP $surf: no $var pin in $pb"; continue; fi; \
        if [ ! -d "$dir/.git" ]; then git clone --quiet --no-tags "$url" "$dir" 2>/dev/null; fi; \
        git -C "$dir" fetch --quiet --tags origin 2>/dev/null || true; \
        if git -C "$dir" checkout --quiet "$pin" 2>/dev/null; then \
            echo "$surf: checked out $pin"; \
        else \
            echo "FAIL $surf: cannot checkout $pin in $dir"; \
        fi; \
    done

# Re-pin the Python uv locks (S2 root, S4 mcp/cluster-exec). Write-prone:
# lands as its own reviewed commit alongside the lockfiles.
deps-tidy-py:
    @uv lock
    @cd mcp/cluster-exec && uv lock

# Emit a candidate SHA bump for the SHA-pinned source trees in the
# build-*.yml playbooks (S6/S7/S9/S10): the current upstream default-branch
# HEAD. Never writes the pin — copy the proposed value into the playbook as its
# own reviewed change, then rebuild + re-audit with deps-vuln-src.
deps-tidy-src:
    @for entry in \
        "podman https://github.com/containers/podman.git config/ansible/admin/build-podman.yml podman_repo_sha" \
        "frp https://github.com/fatedier/frp.git config/ansible/app/build-frp.yml frp_repo_sha"; do \
        set -- $entry; surf="$1"; url="$2"; pb="$3"; var="$4"; \
        cur=$(sed -n "s/^[[:space:]]*${var}:[[:space:]]*[\"']\([^\"']*\)[\"'].*/\1/p" "$pb" | head -n1); \
        next=$(git ls-remote "$url" refs/heads/main 2>/dev/null | cut -f1); \
        echo "$surf: ($pb) current=$cur proposed=$next"; \
    done
    @echo "zulip/open-webui bump via upstream release tag -> new commit SHA in build-zulip.yml / build-open-webui.yml; no auto-write here."

# Regenerate the full machine-readable baseline into tmp/ (gitignored scratch)
# and point the reader at the version-controlled accepted-findings ledger.
# The ledger is the source of truth for consciously accepted items; every time
# you trim it, update docs/design/dependency-hygiene-findings.md by hand.
deps-baseline:
    @mkdir -p ./tmp
    @osv-scanner --lockfile uv.lock --format json > ./tmp/osv-s2.json || true
    @osv-scanner --lockfile mcp/cluster-exec/uv.lock --format json > ./tmp/osv-s4.json || true
    @pip-audit -r config/ansible/admin/files/ycluster/requirements.txt --format json > ./tmp/pip-audit-s3.json || true
    @echo "baseline written to tmp/osv-s2.json, tmp/osv-s4.json, tmp/pip-audit-s3.json"
    @echo "accepted findings ledger: docs/design/dependency-hygiene-findings.md"
