# Eval: should `dev` really be a macOS admin?

`setup-macos.yml` creates `dev` with `group: admin` (primary group), so on
macOS compute nodes `dev` shows up in the `admin` group's membership
alongside `root`/`admin` and effectively has sudo-equivalent rights. On
Linux the intended policy is the opposite — `dev` is deliberately NOT a
sudoer (several Linux playbooks state "dev needs no sudo"). The macOS side
was never explicitly reconciled with that policy; it may just carry over
from how the first Mac was provisioned.

## Question

Does `dev` genuinely need macOS admin rights, or is `root`/`admin` enough
for the services that `become_user: dev` runs on Macs (`brew`, launchd
agents, writing `/opt/homebrew`...)? If not, drop the admin group and run
the relevant sudo-free.

## Links

- Related privileged-set drift: `security/sudo-user-consistency.md`
- Vendor-account guard baseline (macOS allows `dev` today):
  `archive/vendor-account-cleanup.md`
