#!/usr/bin/env bash
# Self-contained test entrypoint for osv-feed. No third-party tooling required.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
PYTHONPATH=. python3 -m unittest discover -s tests -v
