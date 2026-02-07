export LC_ALL=C.UTF-8
export ANSIBLE_VAULT_PASSWORD_FILE=/run/shm/.vault-pass

if [ ! -f "$ANSIBLE_VAULT_PASSWORD_FILE" ]; then
    vault-pass-setup
fi
