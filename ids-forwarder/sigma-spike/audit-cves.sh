#!/usr/bin/env bash
# Audit the crates actually linked into the Linux RSigma release binary.
#
# Cargo.lock can contain stale, dev-only, or other-target packages that are
# not in the shipped binary; querying the whole lockfile overstates exposure.
# This uses `cargo tree -p rsigma --target x86_64-unknown-linux-gnu -e normal`
# and asks OSV about that exact package set.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
RSIGMA_DIR=${RSIGMA_DIR:-"$ROOT/ext/rsigma"}
CARGO=${CARGO:-cargo}
DEPS_FILE="$ROOT/tmp/rsigma-normal-deps.txt"

if [ ! -f "$RSIGMA_DIR/Cargo.lock" ]; then
  echo "RSigma checkout not found at $RSIGMA_DIR (set RSIGMA_DIR)" >&2
  exit 2
fi

(cd "$RSIGMA_DIR" && "$CARGO" tree -p rsigma \
  --target x86_64-unknown-linux-gnu \
  -e normal --prefix none --format '{p}' |
  sed 's/ (\*)$//' | sort -u > "$DEPS_FILE")

python3 - "$DEPS_FILE" <<'PY'
import json
import re
import sys
import urllib.request

pkgs = []
for line in open(sys.argv[1]):
    m = re.match(r"(.+) v(.+)$", line.strip())
    if m:
        pkgs.append((m.group(1), m.group(2)))
if not pkgs:
    sys.exit("no packages parsed from cargo tree output")

queries = [
    {"package": {"name": name, "ecosystem": "crates.io"}, "version": version}
    for name, version in pkgs
]
req = urllib.request.Request(
    "https://api.osv.dev/v1/querybatch",
    data=json.dumps({"queries": queries}).encode(),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req, timeout=60) as resp:
    results = json.load(resp).get("results", [])

rows = []
for (name, version), result in zip(pkgs, results):
    for vuln in result.get("vulns") or []:
        rows.append((name, version, vuln["id"], vuln.get("summary", "")))

print(f"packages={len(pkgs)} vulnerable={len(rows)}")
for row in sorted(rows):
    print(" | ".join(row))
sys.exit(1 if rows else 0)
PY
