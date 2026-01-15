#!/bin/bash
# Wrapper script to run ansible-playbook with vault files
# Usage: ./run-playbook.sh site.yml [additional args...]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAULT_PASS_FILE="${SCRIPT_DIR}/../../.vault_pass"

if [[ ! -f "$VAULT_PASS_FILE" ]]; then
    echo "Error: Vault password file not found at $VAULT_PASS_FILE" >&2
    echo "Create it with: echo 'your-password' > .vault_pass" >&2
    exit 1
fi

ansible-playbook "$@" \
    -e @vault/all.yml \
    -e @vault/storage.yml \
    --vault-password-file "$VAULT_PASS_FILE"
