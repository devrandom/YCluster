#!/bin/bash
# Wrapper script to run ansible-playbook with vault files
# Usage: ./run-playbook.sh site.yml [additional args...]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source env.sh to set LC_ALL and ANSIBLE_VAULT_PASSWORD_FILE
source /etc/ansible/env.sh

ansible-playbook "$@" \
    -e @"${SCRIPT_DIR}/vault/all.yml" \
    -e @"${SCRIPT_DIR}/vault/storage.yml"
