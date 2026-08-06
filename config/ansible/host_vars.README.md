# host_vars is external

`config/ansible/host_vars/` is **not** in this (public) repo. It carries
deployment-specific values (real hostnames, hardware models, PCI addresses,
public ports) that the public repo deliberately excludes.

It lives in the private sibling repo `ycluster-private` and is wired in as a
symlink:

```bash
# from proj/ (the dir containing both repos)
git clone <private-url> ycluster-private
ln -s ../../../ycluster-private/host_vars ycluster/config/ansible/host_vars
```

The symlink path is gitignored here. `dev-sync` uses `rsync
--copy-unsafe-links`, which dereferences this out-of-tree symlink so the real
files land at `config/ansible/host_vars/` on the cluster and Ansible auto-loads
them. In-tree symlinks (`dev/ansible/prod`, `CLAUDE.md`) are unaffected.

Per-node overrides documented elsewhere (e.g. `incus_passthrough_gpu_pci`,
`vm_bastion_service`, `whisperx_enabled`, `cluster_interface`) go in
`ycluster-private/host_vars/<node>.yml`.

## Group vars in the private repo

The same pattern applies to group vars that carry deployment-specific (but
non-secret) values the public repo deliberately excludes. Zulip's admin
contact is the current example, loaded from `group_vars/storage/zulip.yml`:

```bash
# from the public repo root
ln -s ../../../../ycluster-private/group_vars/storage config/ansible/group_vars/storage
```

Note the target needs four `..` levels (it sits under `group_vars/`, one
directory deeper than `host_vars`). `config/ansible/group_vars/storage` is
gitignored, and `dev-sync` dereferences it the same way, so the file reaches
the cluster and Ansible auto-loads it for hosts in the `storage` group.
