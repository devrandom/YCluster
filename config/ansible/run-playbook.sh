#!/bin/bash
# Wrapper to run ansible-playbook with the cluster environment sourced
# (LC_ALL + ANSIBLE_VAULT_PASSWORD_FILE). Vault secrets are loaded per-play
# via vars_files (vault/general.yml, vault/storage.yml) so they reach only the
# plays that need them — not injected globally here.
# Usage: ./run-playbook.sh site.yml [additional args...]

# Source env.sh to set LC_ALL and ANSIBLE_VAULT_PASSWORD_FILE (the latter lets
# the per-play vars_files decrypt at runtime).
source /etc/ansible/env.sh

ansible-playbook "$@"
