# YCluster task runner. Recipes live in just.d/, grouped by concern:
#
#   dev-pxe.just      local PXE/dnsmasq/tftpboot setup
#   completions.just  bash completion symlink
#   tests.just        server-free + dev-cluster system tests
#   lint.just         yamllint + ty + go vet
#   python.just       uv venv + editable ycluster install
#
# `just` with no args lists all recipes. Use `just <recipe>` to run one.

set shell := ["bash", "-uc"]

import "just.d/dev-pxe.just"
import "just.d/completions.just"
import "just.d/tests.just"
import "just.d/lint.just"
import "just.d/worktrees.just"
import "just.d/python.just"

# Default: list every recipe across modules.
default:
    @just --list
