# Vendor account cleanup (done)

Clean stale/vendor-created user accounts on managed nodes (incident:
`../ycluster-private/incidents/2026-06-09 — exx vendor account on nv2`).
Vendor factory-preinstall accounts can ship with full sudo, `lxd`
membership (root-equivalent), lingering login sessions, a populated
`authorized_keys` (potentially holding a cluster key), and a squatted
UID 1000 (forcing `dev` to a non-standard UID) — and a fresh vendor
image comes up unmitigated.

## Manual cleanup (2026-07-25, private `todos.md`)

- `exx` removed with home on nv2 and nv5 (`deluser --remove-home`).
- `/home/exx/.ssh/authorized_keys.disabled` removed on nv2 — it held the
  `ansible@pxe-server` cluster key.

## Automated sweep (bootstrap guard)

The non-PXE node-type bootstraps now fail bring-up if a non-baseline
privileged account is present on a fresh vendor image, before the node
creates `admin`, installs the SSH key, or is registered:

- `nvidia-bootstrap.sh.j2`, `nas-bootstrap.sh.j2` — fail if any
  non-baseline UID>=1000 account is a member of `sudo` or `lxd`.
- `macos-bootstrap.sh.j2` — fail if any non-baseline account is a member
  of the macOS `admin` group.
- Remote (WireGuard) paths are guarded too: `wg-bootstrap.sh.j2` (sudo/lxd)
  and `wg-macos-bootstrap.sh.j2` (macOS `admin`), skipped in `--dev` mode.

Baseline privileged account is `root` + `admin` on Linux and
`root` + `admin` + `dev` on macOS (`setup-macos.yml` sets `dev`'s primary
group to `admin`). Any other privileged account must be removed (or the
node re-imaged) before bootstrap will proceed.

## Real-machine validation (2026-08-05)

- Guard executed against live systems. m1 (macOS): passes — `dev` in the
  `admin` group is allowed as baseline. nv1 (Linux): fails, correctly
  flagging `dev` in `sudo` and `lxd`.
- The nv1 `dev`-in-`sudo`/`lxd` is drift: no playbook adds `dev` to either
  on Linux (`setup-base-compute.yml` adds only `render`,`video`;
  `install-incus.yml` adds `incus-admin` and removes the `lxd` snap;
  `install-glm52.yml` states dev needs no sudo). Tracked under
  `sudo-user-consistency`.
- `dscl . -read /Groups/admin GroupMembership` on a live Mac returns names
  (`root dev admin`), matching the guard's parser.

## Remaining (not this item)

Operator-manual `foresight` removal on c3/m3 (name kept out of code) and
`ubuntu` on c1/c2 plus the nv3 re-survey are tracked under the paired
`sudo-user-consistency` item.
