# Backup restore subcommand

Add a `restore` subcommand to
`config/ansible/storage/scripts/backup-databases` (or sibling script)
covering:

- `pg_dumpall`
- Qdrant snapshot
- `etcdctl snapshot restore`

Document the procedure end-to-end including age decryption.
