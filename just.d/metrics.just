# Quality-metric gate (interlinked-style ratchet), no Node/TS required.
#
#   just metrics          gate: fail on NEW violations over cap
#   just metrics-adopt    re-freeze the baseline after intentional cleanup
#   just metrics-mutate   mutation testing (gremlins); gate requires 0 survivors
#   just metrics-tools    install/verify the pinned analysis tools
#
# Caps live in scripts/metrics/caps.json; the frozen legacy baseline in
# scripts/metrics/baseline.json. Tools are pinned at module versions
# (scripts/metrics/go-tools.txt, checksum-verified against sum.golang.org
# at download time) and by --require-hashes for the Python side
# (scripts/metrics/requirements.txt).

py := ".venv/bin/python"

metrics:
    @{{py}} scripts/metrics/check.py

metrics-adopt:
    @{{py}} scripts/metrics/check.py --adopt

metrics-mutate:
    #!/bin/bash
    set -euo pipefail
    LOG="$(pwd)/tmp/metrics-mutants.log"
    ROOT="$(pwd)"
    # Keep gremlins/go builds off the (small) /tmp tmpfs. GOPATH must be
    # absolute: a relative one resolves against the cwd, so commands run in
    # local-ai-proxy would drop their module cache inside the module tree,
    # where the metric tools' recursive scans then choke on it.
    mkdir -p "$ROOT/tmp/go-cache" "$ROOT/tmp/gotmp" "$ROOT/tmp/gopath"
    export GOCACHE="$ROOT/tmp/go-cache" GOTMPDIR="$ROOT/tmp/gotmp" TMPDIR="$ROOT/tmp/gotmp"
    export GOPATH="$ROOT/tmp/gopath"
    (cd local-ai-proxy && "$ROOT/tmp/go-bin/gremlins" unleash 2>&1) | tee "$LOG"
    # gremlins leaves a module cache inside the module dir when GOPATH is
    # relative; sweep any residue so the metric scans stay clean.
    if [ -d local-ai-proxy/tmp ]; then
        chmod -R u+w local-ai-proxy/tmp 2>/dev/null || true
        rm -rf local-ai-proxy/tmp
    fi
    # Gate: zero surviving mutants (gremlins summary line is comma-joined:
    # "Killed: N, Lived: N, Not covered: N").
    if grep -E "\bLived: [1-9]" "$LOG" >/dev/null; then
        echo "FAIL: surviving mutants above 0 (see $LOG)" >&2
        exit 1
    fi

metrics-tools:
    #!/bin/bash
    set -euo pipefail
    mkdir -p ./tmp/go-cache ./tmp/go-bin ./tmp/gotmp
    export GOCACHE="$(pwd)/tmp/go-cache" GOBIN="$(pwd)/tmp/go-bin" GOTMPDIR="$(pwd)/tmp/gotmp"
    while read -r mod; do
        [ -z "$mod" ] && continue
        case "$mod" in \#*) continue ;; esac
        echo "go install $mod"
        go install "$mod"
    done < scripts/metrics/go-tools.txt
    # uv is preferred; fall back to pip in the venv (same --require-hashes pin).
    uv pip install --python .venv/bin/python --require-hashes -r scripts/metrics/requirements.txt \
        || { .venv/bin/python -m ensurepip --upgrade >/dev/null 2>&1 || true
             .venv/bin/python -m pip install --require-hashes -r scripts/metrics/requirements.txt; }
    echo "metrics tooling verified"
