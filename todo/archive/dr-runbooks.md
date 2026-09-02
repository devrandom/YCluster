# Disaster-recovery runbooks

Write disaster-recovery runbooks for the cases the existing docs punt on:

- etcd quorum loss (minority survivors)
- Ceph quorum loss / corruption
- Full cluster cold-start
- VIP split-brain recovery

`docs/operations/etcd.md` and `ceph.md` explicitly reference DR
procedures that don't exist. Detailed plan split across the DR/backups
files in this directory (`backup-restore-subcommand.md`,
`test-restore-job.md`, `ceph-off-cluster-backup.md`,
`age-key-custody-doc.md`).
