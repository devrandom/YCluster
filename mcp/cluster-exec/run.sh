#!/usr/bin/env bash
# Wrapper for the cluster_exec MCP server.
#
# Two responsibilities:
#   1. Suppress uv's "VIRTUAL_ENV does not match --project" warning. The root
#      .envrc exports VIRTUAL_ENV to the ycluster root venv; when opencode
#      invokes us with --project pointing at this sub-package's venv, uv
#      warns but still uses the sub-package venv. We just unset the stale
#      var so uv has nothing to complain about.
#   2. Bridge the project's BASTION env var to the MCP server's CE_BASTION.
#      The MCP server itself is project-agnostic and only reads CE_BASTION;
#      this wrapper does the translation so operators don't have to.
set -euo pipefail
unset VIRTUAL_ENV
export CE_BASTION="${CE_BASTION:-${BASTION:-}}"
exec uv run --project "$(dirname "$(readlink -f "$0")")" python -m cluster_exec "$@"
