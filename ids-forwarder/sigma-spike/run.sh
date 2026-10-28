#!/usr/bin/env bash
# Offline RSigma spike: Tetragon JSON -> normalized Sigma events -> detections.
#
# Requirements:
#   - Go (the bridge builds with the repo's ids-forwarder module)
#   - jq and python3
#   - RSigma v0.25.0 built from source at ext/rsigma (see README.md)
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SPIKE="$ROOT/ids-forwarder/sigma-spike"
RULES="$ROOT/config/ansible/monitoring/files/rsigma"
OUT="$SPIKE/out"
RSIGMA_BIN=${RSIGMA_BIN:-"$ROOT/ext/rsigma/target/release/rsigma"}

if [ ! -x "$RSIGMA_BIN" ]; then
  echo "RSigma binary not found at $RSIGMA_BIN" >&2
  echo "Build it from source first: see ids-forwarder/sigma-spike/README.md" >&2
  exit 2
fi

mkdir -p "$OUT"
(cd "$ROOT/ids-forwarder" && go build -o "$ROOT/tmp/ids-sigma-bridge" ./cmd/ids-sigma-bridge)

"$ROOT/tmp/ids-sigma-bridge" \
  -input "$SPIKE/samples/tetragon.jsonl" \
  -output "$OUT/normalized.ndjson"

"$RSIGMA_BIN" engine eval \
  -r "$RULES/rules" \
  -p "$RULES/pipeline.yml" \
  -e @"$OUT/normalized.ndjson" \
  --include-event \
  --no-stats \
  --output-format ndjson \
  > "$OUT/detections.ndjson" 2> "$OUT/eval.err"

python3 - "$SPIKE/samples/expected.json" "$OUT/detections.ndjson" <<'PY'
import json
import sys

expected = json.load(open(sys.argv[1]))
actual = {pid: [] for pid in expected}
for line in open(sys.argv[2]):
    line = line.strip()
    if not line:
        continue
    det = json.loads(line)
    pid = str(det["event"]["ProcessId"])
    actual.setdefault(pid, []).append(det["rule_title"])
for pid in actual:
    actual[pid].sort()

if actual != expected:
    print("detection mismatch", file=sys.stderr)
    for pid in sorted(expected, key=int):
        if actual.get(pid) != expected[pid]:
            print(f"  pid {pid}: want {expected[pid]}, got {actual.get(pid)}", file=sys.stderr)
    sys.exit(1)

total = sum(len(v) for v in expected.values())
print(f"ok: {total} detections across {len(expected)} events, no benign matches")
PY

echo "detections: $OUT/detections.ndjson"
echo "rsigma log: $OUT/eval.err"
