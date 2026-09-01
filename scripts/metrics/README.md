# Quality-metric gate

An interlinked-style metric ratchet for this repo — no Node/TypeScript
required. Run `just metrics` (gate) or `just metrics-adopt` (re-freeze the
baseline after deliberate cleanup). The Python side runs through the repo's
uv-managed venv (`.venv`); tool install is `just metrics-tools`, which uses
`uv pip install --require-hashes`.

## Caps

Defined in `caps.json`:

| Metric | Cap | Scope | Source (Go / Python) |
|---|---|---|---|
| Cyclomatic complexity | < 22 | per function, non-test | `gocyclo` / `radon cc` |
| Cognitive complexity | < 22 | per function, non-test | `gocognit` / `radon cc` |
| Halstead difficulty | < 80 | per file | `scripts/metrics/halstead.go` / `radon hal` |
| Lines of code | < 500 | per file, non-test (SLOC) | line count / `radon raw` |
| CRAP | < 25 | per function, non-test | cyclomatic² · (1 − coverage)³ + cyclomatic |
| Test coverage | 95% (goal) | per file | `go tool cover` / `coverage.py` |
| Dead code | 0 | repo-wide count, ratcheted | `staticcheck U1000` / `vulture` |
| `any`/`interface{}` | 0 | non-test Go source, ratcheted | grep pass in `check.py` |

## Ratchet semantics

A violation fails the gate only when it exceeds **both** the cap **and** the
recorded baseline (`baseline.json`). Legacy debt is frozen: existing
over-cap code may not grow, and any new code is held to the caps. Clearing
debt shrinks the baseline via `just metrics-adopt`.

CRAP for uncovered Go functions assumes 0% coverage. Python CRAP uses
per-function line coverage from coverage.py (via radon's function line
ranges) over the unittest suites in `run-tests.sh` plus the monitoring
collector tests.

**Scope note:** every `.py` under `config/ansible` is scanned except test
files. Inline `python3 -c` blocks embedded in playbooks (e.g. the
usage-stats password bootstrap in `admin-stats.yml`) are not scannable;
move such logic into the `ycluster` package where it gets scanned and tested.

## Tool pinning / auditability

- Python deps: `scripts/metrics/requirements.txt`, installed with
  `uv pip install --require-hashes` — every artifact is pinned to its PyPI
  sha256 digest.
- Go tools: `scripts/metrics/go-tools.txt` pins module@version. `go install`
  verifies against sum.golang.org, and `scripts/metrics/go-tools.sha256`
  records the expected binary hashes for this platform,
  verified by `just metrics-tools`.
- `scripts/metrics/halstead.go` is stdlib-only and executed via `go run`, so
  it is pinned by being in-tree.
- Mutation testing (`just metrics-mutate`) uses `gremlins` (pinned v0.6.0)
  and requires zero surviving mutants. It is not part of the default gate
  because of runtime cost; run it before risky changes land.

## Layout

    scripts/metrics/caps.json          thresholds
    scripts/metrics/baseline.json      frozen legacy debt (ratchet)
    scripts/metrics/check.py           the gate
    scripts/metrics/halstead.go        Go Halstead difficulty (go run)
    scripts/metrics/go-tools.txt       pinned Go tool versions
    scripts/metrics/go-tools.sha256    expected binary hashes (linux/amd64)
    scripts/metrics/requirements.txt   pinned Python deps + sha256s
