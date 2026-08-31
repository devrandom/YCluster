#!/usr/bin/env python3
"""Quality-metric gate (interlinked-style ratchet) for ycluster surfaces.

Enforced caps (scripts/metrics/caps.json):
    cyclomatic < 22        per function (gocyclo / radon cc)
    cognitive  < 22        per function (gocognit / radon cc)
    halstead   < 80        per file difficulty (token scan / radon hal)
    lines      < 500       per source file (sloc)
    crap       < 25        per function, cyclomatic^2*(1-cov)^3 + cyclomatic
    coverage   = 100%      goal; ratcheted (per-file)
    dead code  = 0         ratcheted count (staticcheck U1000 / vulture)
    any types  = 0         ratcheted count (Go `interface{}`/`any` in non-test src)

Usage:
    python3 scripts/metrics/check.py            # gate: fail on NEW violations
    python3 scripts/metrics/check.py --adopt    # re-record baseline after cleanup
    python3 scripts/metrics/check.py --skip-coverage  # skip test run (no CRAP/coverage)

A violation fails the gate only if it exceeds BOTH the cap and the recorded
baseline, so legacy debt is frozen while new code is held to the cap. Adopt
the baseline deliberately after clearing debt.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
METRICS_DIR = ROOT / "scripts" / "metrics"
CAPS_PATH = METRICS_DIR / "caps.json"
BASELINE_PATH = METRICS_DIR / "baseline.json"
GO_DIR = ROOT / "local-ai-proxy"
PY_PATHS = [
    ROOT / "config" / "ansible" / "admin" / "files",
    ROOT / "config" / "ansible" / "monitoring" / "files",
    ROOT / "config" / "ansible" / "app" / "files",
    ROOT / "config" / "ansible" / "macos" / "files",
    ROOT / "config" / "ansible" / "scripts",
    ROOT / "config" / "ansible" / "inventory_plugins",
]
BIN = ROOT / "tmp" / "go-bin"

# .codex env may set a relative GOPATH; subprocesses run from local-ai-proxy
# would then resolve it inside the module tree and pollute the scans.
_gopath = os.environ.get("GOPATH", "")
if _gopath and not os.path.isabs(_gopath):
    os.environ["GOPATH"] = str(ROOT / _gopath)


def run(cmd: list[str], cwd: Path | None = None, env: dict | None = None) -> str:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    if p.returncode != 0:
        raise RuntimeError(
            f"command failed ({p.returncode}): {' '.join(cmd)}\n{p.stderr}{p.stdout}"
        )
    return p.stdout


def run_findings(
    cmd: list[str], cwd: Path | None = None, findings_codes: set[int] = frozenset({1})
) -> str:
    """Run a linter that exits nonzero when it finds something.

    ``findings_codes`` are exit codes that mean "ran fine, found issues";
    any other nonzero exit is a tool failure and raises, so a broken linter
    can never look like a clean pass. stdout/stderr are surfaced on failure.
    """
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        # Findings exits (1) carry diagnostics on stdout and nothing on stderr;
        # a nonzero exit WITH stderr output is a tool failure (e.g. staticcheck
        # exits 1 both for findings and for packages that fail to load).
        if p.returncode not in findings_codes or p.stderr.strip():
            raise RuntimeError(
                f"command failed ({p.returncode}): {' '.join(cmd)}\n{p.stderr}{p.stdout}"
            )
    return p.stdout


def load_json_bytes(path: Path) -> dict:
    return json.loads(path.read_text())


def crap(cyc: float, cov: float) -> float:
    """cov in [0,1]."""
    return cyc * cyc * (1 - cov) ** 3 + cyc


# ---------------------------------------------------------------- Go side ---

def go_cyclomatic() -> dict[tuple[str, str], float]:
    """Cyclomatic per function, keyed by (file, name) — stable across edits."""
    out = run([str(BIN / "gocyclo"), "-over", "0", "."], cwd=GO_DIR)
    res = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        cyc = int(parts[0])
        # format: <cyc> <pkg> <func> <file>:<line>:<col>
        name = parts[2]
        pos = parts[-1]
        file_part, _line_no, _ = pos.rsplit(":", 2)
        if file_part.endswith("_test.go") or "/tmp/" in file_part:
            continue
        res[(file_part, name)] = cyc
    return res


def go_cognitive() -> dict[tuple[str, str], float]:
    out = run([str(BIN / "gocognit"), "-json", "-over", "0", "."], cwd=GO_DIR)
    res = {}
    for fn in json.loads(out):
        pos = fn["Pos"]
        if pos["Filename"].endswith("_test.go") or "/tmp/" in pos["Filename"]:
            continue
        res[(pos["Filename"], fn["FuncName"])] = fn["Complexity"]
    return res


def go_halstead() -> dict[str, float]:
    out = run(["go", "run", str(METRICS_DIR / "halstead.go"), "."], cwd=GO_DIR)
    return {r["file"]: r["difficulty"] for r in json.loads(out)}


def go_dead_code() -> list[str]:
    # staticcheck exits 1 for findings; other exits are tool errors.
    out = run_findings(
        [str(BIN / "staticcheck"), "-checks", "U1000", "./..."],
        cwd=GO_DIR,
        findings_codes={1},
    )
    return [line for line in out.splitlines() if line.strip()]


def go_coverage(profile: Path) -> tuple[dict[tuple[str, str], float], float | None]:
    """Return (per-func coverage keyed by (file, name), total fraction)."""
    run(["go", "test", "./...", "-coverprofile=" + profile.name], cwd=GO_DIR)
    out = run(["go", "tool", "cover", "-func", profile.name], cwd=GO_DIR)
    per_func: dict[tuple[str, str], float] = {}
    total = None
    for line in out.splitlines():
        if not line.strip():
            continue
        if line.startswith("total:"):
            total = float(line.rsplit("\t", 1)[1].rstrip("%")) / 100
            continue
        pos, name, pct = line.rsplit("\t", 2)
        frac = float(pct.rstrip("%")) / 100
        # pos is <module>/local-ai-proxy/<file>:<line>; match gocyclo's
        # module-relative paths.
        file_part = pos.rsplit(":", 1)[0]
        marker = "local-ai-proxy/"
        if marker in file_part:
            file_part = file_part.split(marker, 1)[1]
        per_func[(file_part, name)] = frac
    return per_func, total


def _strip_go_noncode(src: str) -> str:
    """Remove comments and string/rune literals so prose can't match."""
    out = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            i = src.find("\n", i)
            i = n if i == -1 else i
        elif ch == "/" and nxt == "*":
            end = src.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif ch in ('"', "'", "`"):
            quote = ch
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == quote or (quote != "`" and src[i] == "\n"):
                    break
                i += 1
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def go_any_types() -> list[str]:
    """Flag `interface{}` / `any` type usages in non-test Go source.

    Comments and literals are stripped first, so `\\bany\\b` matches real type
    positions (`map[string]any`, `[]any`, bare `any`) and not English prose.
    """
    hits = []
    for path in sorted(GO_DIR.rglob("*.go")):
        if path.name.endswith("_test.go") or "vendor" in path.parts:
            continue
        code = _strip_go_noncode(path.read_text())
        for i, line in enumerate(code.splitlines(), 1):
            if re.search(r"\bany\b", line) or "interface{}" in line:
                hits.append(
                    f"{path.relative_to(ROOT)}:{i}: any/interface{{}} use"
                )
    return hits


def _stable_key(hit: str) -> str:
    """Map a dead-code finding to a stable identifier.

    Line numbers shift with unrelated edits, so the ratchet keys on
    file + symbol (falling back to the raw line when a tool's format
    changes). staticcheck: "f.go:1:6: func X is unused (U1000)".
    vulture: "f.py:12: unused function 'x' (60% confidence)".
    """
    m = re.match(
        r"(\S+?):\d+(?::\d+)?: (?:func|type|const|var) (\S+) is unused", hit
    )
    if m:
        return f"{m.group(1)}::{m.group(2)}"
    m = re.match(r"(\S+?):\d+: unused (\w+) '([^']+)'", hit)
    if m:
        return f"{m.group(1)}::{m.group(2)}:{m.group(3)}"
    return hit


def collect_go(skip_coverage: bool) -> list[tuple[str, str, float]]:
    """Return violation candidates: (metric, key, value)."""
    out = []
    cyc = go_cyclomatic()
    cog = go_cognitive()
    hal = go_halstead()

    cov_by_func: dict[tuple[str, int], float] = {}
    cov_total = None
    if not skip_coverage:
        profile = GO_DIR / "metrics.cover.out"
        cov_by_func, cov_total = go_coverage(profile)
        profile.unlink(missing_ok=True)

    # Per-function metrics are keyed by (file, name); all three Go tools
    # report the same names for the same functions.
    for key, c in cyc.items():
        name = f"{key[0]}::{key[1]}"
        out.append(("cyclomatic", name, c))
        if key in cog:
            out.append(("cognitive", name, cog[key]))
        if key in cov_by_func:
            out.append(("crap", name, round(crap(c, cov_by_func[key]), 1)))
        if key not in cov_by_func and not skip_coverage:
            # untested function == 0 coverage for CRAP
            out.append(("crap", name, round(crap(c, 0.0), 1)))
    for f, d in hal.items():
        out.append(("halstead", f, round(d, 1)))
    for path in sorted(GO_DIR.rglob("*.go")):
        if path.name.endswith("_test.go"):
            continue
        n = len(path.read_text().splitlines())
        out.append(("lines", str(path.relative_to(ROOT)), n))
    # any_types: keyed per file with a count so line shifts don't re-fail;
    # adding a new `any`/`interface{}` raises the file's count past baseline.
    any_hits: dict[str, int] = {}
    for hit in go_any_types():
        any_hits[hit.split(":")[0]] = any_hits.get(hit.split(":")[0], 0) + 1
    for f, n in sorted(any_hits.items()):
        out.append(("any_types", f, n))
    dead = go_dead_code()
    for hit in dead:
        out.append(("dead_code", _stable_key(hit), 1))
    if cov_total is not None:
        out.append(("coverage", "local-ai-proxy total", cov_total * 100))
    return out


# ------------------------------------------------------------- Python side ---

def _py_source(paths: list[str]) -> list[str]:
    """Expand scan roots to non-test .py files (test files are exempt)."""
    keep = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            files = sorted(path.rglob("*.py"))
        else:
            files = [path]
        for f in files:
            if f.name.startswith("test_") or f.name.endswith("_test.py"):
                continue
            if "tests" in f.parts:
                continue
            if "__pycache__" in f.parts:
                continue
            # Vendored patch modules monkey-patch upstream sglang; their
            # "unused" symbols mirror upstream signatures (same as vendor).
            if "glm53-sglang" in f.parts and "patches" in f.parts:
                continue
            keep.append(str(f))
    return keep


def py_coverage(
    blocks: list[tuple[str, list[dict]]],
) -> tuple[dict[str, float], dict[tuple[str, str], float]]:
    """Run coverage.py over the unittest suites.

    blocks: [(relpath, radon cc blocks)] used for per-function CRAP.
    Returns ({relpath: pct}, {(relpath, name): func coverage fraction}).
    """
    import os

    data_file = METRICS_DIR / ".coverage-py"
    json_path = METRICS_DIR / "coverage-py.json"
    env = dict(os.environ)
    env["COVERAGE_FILE"] = str(data_file)
    files_root = ROOT / "config" / "ansible" / "admin" / "files"
    # Suites matching run-tests.sh plus the monitoring collector tests.
    suites = (
        (files_root, {}, ["discover"]),
        (files_root / "ycluster", {"PYTHONPATH": "."}, ["discover"]),
        (
            ROOT / "config" / "ansible" / "monitoring" / "files",
            {},
            [
                "test_crps_collector",
                "test_node_temps_collector",
                "test_ntfy_webhook",
                "test_osv_feed_collector",
            ],
        ),
    )
    for i, (cwd, extra_env, targets) in enumerate(suites):
        e = dict(env)
        e.update(extra_env)
        if targets == ["discover"]:
            cmd = ["coverage", "run"] + (["--append"] if i else []) + [
                "--source", ".", "-m", "unittest", "discover", "-s", "tests"]
        else:
            cmd = ["coverage", "run", "--append", "--source", ".",
                   "-m", "unittest", *targets]
        run(cmd, cwd=cwd, env=e)
    run(["coverage", "json", "-o", str(json_path)], env=env)
    data = load_json_bytes(json_path)

    missing: dict[str, set[int]] = {}
    counts: dict[str, tuple[int, int]] = {}
    for path, summary in data["files"].items():
        p = str((ROOT / path).resolve())
        try:
            rel = str(Path(p).relative_to(ROOT))
        except ValueError:
            continue
        missing[rel] = set(summary["missing_lines"])
        if "/tests/" in f"/{rel}" or Path(rel).name.startswith("test_"):
            continue  # test files don't count toward the coverage goal
        s = summary["summary"]
        counts[rel] = (s["covered_lines"], s["num_statements"])

    per_file = {
        f: round(100 * c / t, 1) for f, (c, t) in counts.items() if t
    }
    per_func: dict[tuple[str, str], float] = {}
    for rel, fns in blocks:
        m = missing.get(rel, set())
        for b in fns:
            if b["type"] != "function":
                continue
            total_lines = b["endline"] - b["lineno"] + 1
            hit = sum(1 for ln in range(b["lineno"], b["endline"] + 1) if ln not in m)
            per_func[(rel, b["name"])] = hit / total_lines if total_lines else 0.0
    json_path.unlink(missing_ok=True)
    data_file.unlink(missing_ok=True)
    return per_file, per_func


def collect_py(skip_coverage: bool) -> list[tuple[str, str, float]]:
    out = []
    paths = _py_source([str(p) for p in PY_PATHS if p.exists()])
    if not paths:
        return out
    cc = json.loads(run(["radon", "cc", "-j", "-s", *paths]))
    blocks: list[tuple[str, list[dict]]] = []
    for path, fns in cc.items():
        rel = str(Path(path).relative_to(ROOT))
        blocks.append((rel, fns))
        for b in fns:
            if b["type"] not in ("function", "method"):
                continue
            out.append(("cyclomatic", f"{rel}::{b['name']}", b["complexity"]))
    hal = json.loads(run(["radon", "hal", "-j", *paths]))
    for path, m in hal.items():
        rel = str(Path(path).relative_to(ROOT))
        out.append(("halstead", rel, round(m["total"]["difficulty"], 1)))
    raw = json.loads(run(["radon", "raw", "-j", *paths]))
    for path, m in raw.items():
        rel = str(Path(path).relative_to(ROOT))
        out.append(("lines", rel, m["sloc"]))
    # vulture exits 3 when dead code is found (1 = invalid input, 2 = bad args).
    whitelist = METRICS_DIR / "whitelist.py"
    vuln = run_findings(["vulture", *paths, str(whitelist)], findings_codes={3})
    for line in vuln.splitlines():
        if line.strip():
            out.append(("dead_code", _stable_key(line.strip()), 1))

    if skip_coverage:
        return out
    per_file, per_func = py_coverage(blocks)
    for rel, pct in per_file.items():
        out.append(("coverage", rel, pct))
    cyc_by_key = {
        (rel, b["name"]): b["complexity"]
        for rel, fns in blocks
        for b in fns
        if b["type"] in ("function", "method")
    }
    for key, cov in per_func.items():
        c = cyc_by_key.get(key)
        if c is None:
            continue
        name = f"{key[0]}::{key[1]}"
        out.append(("crap", name, round(crap(c, cov), 1)))
    return out


# ------------------------------------------------------------------ gate ---

def check(adopt: bool, skip_coverage: bool) -> int:
    caps = load_json_bytes(CAPS_PATH)
    baseline = load_json_bytes(BASELINE_PATH) if BASELINE_PATH.exists() else {}

    candidates = collect_go(skip_coverage) + collect_py(skip_coverage)

    # "lower-is-stricter" metrics; coverage is a goal ratcheted upward.
    violations: list[tuple[str, str, float, float | None]] = []
    new_baseline: dict[str, dict[str, float]] = {}
    for metric, key, value in candidates:
        cap = caps.get(metric)
        if cap is None:
            continue
        bl = new_baseline.setdefault(metric, {})
        if metric == "coverage":
            # lower-is-stricter: baseline keeps the best (max) observed coverage
            if key in bl and bl[key] >= value:
                continue
        elif key in bl and bl[key] >= value:
            continue
        bl[key] = value
        base = baseline.get(metric, {}).get(key)
        if metric == "coverage":
            if value < cap and (base is None or value < base):
                violations.append((metric, key, value, base))
        elif value >= cap and (base is None or value > base):
            violations.append((metric, key, value, base))

    if adopt:
        BASELINE_PATH.write_text(json.dumps(new_baseline, indent=1, sort_keys=True) + "\n")
        print(f"baseline adopted -> {BASELINE_PATH.relative_to(ROOT)}")

    if violations:
        print(f"\n{len(violations)} NEW violation(s) over cap:")
        for metric, key, value, base in sorted(violations):
            bl = f" (baseline {base})" if base is not None else ""
            print(f"  {metric:11s} {value:>8}  {key}{bl}")
        return 1
    print("metrics: no new violations (legacy debt frozen at baseline)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adopt", action="store_true", help="re-record the baseline")
    ap.add_argument("--skip-coverage", action="store_true", help="skip go test/coverage run")
    args = ap.parse_args()
    if args.adopt and args.skip_coverage:
        print(
            "error: --adopt with --skip-coverage would erase the coverage and "
            "CRAP baselines; run a full --adopt without --skip-coverage",
            file=sys.stderr,
        )
        return 2
    try:
        return check(args.adopt, args.skip_coverage)
    except RuntimeError as e:
        print(f"metrics gate error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
