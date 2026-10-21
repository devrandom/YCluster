#!/usr/bin/env bash
# Stage operator-supplied ComfyUI model files from a TSV manifest.
#
# This is the content-agnostic companion to seed-comfyui-models.sh: the built-in
# seeder owns the default model families, while this script stages an additional
# manifest supplied by the operator. The manifest and the files it references stay
# outside the public repo.
#
#   MANIFEST=/path/to/models.tsv seed-comfyui-extra-models.sh
#   MANIFEST=/path/to/models.tsv seed-comfyui-extra-models.sh --download
#
# Each row is tab-separated:
#   id  repo  revision  repo_path  destination_relative_path  sha256
#
# Use "skip" for sha256 only when the upstream file has no stable hash. Blank
# lines and lines starting with # are ignored. Destination paths are relative to
# MODEL_ROOT and may not escape it.
set -euo pipefail

MANIFEST="${MANIFEST:-}"
MODEL_ROOT="${MODEL_ROOT:-/mnt/models}"
STAGE_SUBDIR="${STAGE_SUBDIR:-.seed-staging-extra}"
STAGE_ROOT="${MODEL_ROOT}/${STAGE_SUBDIR}"

action="verify"
while [ $# -gt 0 ]; do
  case "$1" in
    --download)     action="download"; shift ;;
    --verify-only)  action="verify"; shift ;;
    -h|--help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$MANIFEST" ]; then
  echo "MANIFEST is required (set MANIFEST=/path/to/models.tsv)" >&2
  exit 1
fi
if [ ! -f "$MANIFEST" ] || [ ! -r "$MANIFEST" ]; then
  echo "manifest not found or unreadable: $MANIFEST" >&2
  exit 1
fi

mkdir -p "$MODEL_ROOT" "$STAGE_ROOT"
exec 9>"$STAGE_ROOT/.lock"
if ! flock -n 9; then
  echo "another extra-model seeder is already running under $MODEL_ROOT" >&2
  exit 1
fi

missing=0
rows=0

validate_id() {
  case "$1" in
    ""|*[!A-Za-z0-9._-]*)
      echo "invalid manifest id: '$1'" >&2
      return 1
      ;;
  esac
}

validate_rel_path() {
  local label="$1" value="$2"
  case "$value" in
    ""|/*|*..*)
      echo "invalid $label path: '$value'" >&2
      return 1
      ;;
  esac
}

validate_hash() {
  local value="$1"
  if [ "$value" = "skip" ]; then
    return 0
  fi
  case "$value" in
    *[!0-9a-f]*)
      echo "invalid sha256: '$value'" >&2
      return 1
      ;;
  esac
  if [ "${#value}" -ne 64 ]; then
    echo "invalid sha256 length: '$value'" >&2
    return 1
  fi
}

verify_file() {
  local file="$1" expected="$2"
  if [ ! -f "$file" ]; then
    echo "  missing: $file" >&2
    return 1
  fi
  if [ "$expected" = "skip" ]; then
    echo "  present: $file"
    return 0
  fi
  local actual
  actual=$(sha256sum "$file" | awk '{print $1}')
  if [ "$actual" != "$expected" ]; then
    echo "  hash mismatch: $file" >&2
    echo "    expected: $expected" >&2
    echo "    actual:   $actual" >&2
    return 1
  fi
  echo "  verified: $file"
}

download_one() {
  local id="$1" repo="$2" revision="$3" source_rel="$4" dest_rel="$5" expected="$6"
  local final="$MODEL_ROOT/$dest_rel"
  local stage="$STAGE_ROOT/$id"

  echo "== $id =="
  if verify_file "$final" "$expected"; then
    return 0
  fi
  if [ "$action" != "download" ]; then
    missing=1
    return 0
  fi

  mkdir -p "$stage" "$MODEL_ROOT/$(dirname "$dest_rel")"
  # HF_HUB_DISABLE_XET=1 keeps large files on the regular CDN. The compute
  # nodes stall against the Xet CAS endpoint, as with the built-in seeder.
  HF_HUB_DISABLE_XET=1 uv tool run --from huggingface-hub==1.24.0 hf download \
    "$repo" \
    --revision "$revision" \
    --include "$source_rel" \
    --local-dir "$stage"

  local staged="$stage/$source_rel"
  if [ ! -f "$staged" ]; then
    echo "download did not create $staged" >&2
    missing=1
    return 0
  fi
  if [ "$expected" != "skip" ]; then
    local actual
    actual=$(sha256sum "$staged" | awk '{print $1}')
    if [ "$actual" != "$expected" ]; then
      echo "downloaded hash mismatch for $id" >&2
      echo "  expected: $expected" >&2
      echo "  actual:   $actual" >&2
      missing=1
      return 0
    fi
  fi
  mv -f "$staged" "$final"
  chmod 0644 "$final"
  rmdir "$(dirname "$staged")" "$stage" 2>/dev/null || true
  verify_file "$final" "$expected"
}

echo "== ${action} under $MODEL_ROOT (manifest: $MANIFEST) =="
while IFS=$'\t' read -r id repo revision source_rel dest_rel expected; do
  id="${id%$'\r'}"
  repo="${repo%$'\r'}"
  revision="${revision%$'\r'}"
  source_rel="${source_rel%$'\r'}"
  dest_rel="${dest_rel%$'\r'}"
  expected="${expected%$'\r'}"
  [ -z "$id" ] && continue
  case "$id" in \#*) continue ;; esac

  rows=$((rows + 1))
  validate_id "$id" || exit 1
  validate_rel_path "source" "$source_rel" || exit 1
  validate_rel_path "destination" "$dest_rel" || exit 1
  validate_hash "$expected" || exit 1
  if [ -z "$repo" ] || [ -z "$revision" ]; then
    echo "invalid manifest row for $id: repo and revision are required" >&2
    exit 1
  fi

  download_one "$id" "$repo" "$revision" "$source_rel" "$dest_rel" "$expected"
done < "$MANIFEST"

if [ "$rows" -eq 0 ]; then
  echo "manifest has no entries: $MANIFEST" >&2
  exit 1
fi
if [ "$missing" -ne 0 ]; then
  echo "Incomplete model tree at $MODEL_ROOT — re-run with --download" >&2
  exit 1
fi
echo
echo "Extra model tree is complete."
