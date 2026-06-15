# macOS Compute Node Setup

## Initial Install

Perform initial setup on the Mac, erasing any old data if present.

When prompted, create a user `admin` used for first log-in.  Skip all optional software configuration, apple account sign-in, sending of any info to Apple, etc.

For software updates, choose "Only download automatically".

## Bootstrap

Log-in to the UI.  Grant "Full Disk Access" to `Terminal`: **System Settings → Privacy & Security → Full Disk Access**.

Run the bootstrap script:

```bash
curl -sf http://admin.xc/macos/bootstrap | sudo bash
```

This allocates a hostname (m1, m2, etc.) and IP from etcd, creates an `admin` user with SSH key auth, enables Remote Login, and disables sleep.

Prerequisites: macOS connected to cluster network on `en0`, `jq` installed.

## IP Allocation

macOS nodes get IPs in `10.0.0.91-110` (m1=10.0.0.91, m2=10.0.0.92, etc.)

## Run Ansible and Reboot

Run Ansible on the new node.  Afterward, you may have to reboot before the services (launch configurations) run properly. 

## Xcode + Metal Toolchain

Required only on nodes that will run MLX-based workloads (exo, vllm-mlx,
mlx-lm) — these JIT-compile Metal shaders and need `xcrun metal` to
work. The standalone Command Line Tools do not include the Metal shader
compiler.

```bash
# As dev, install Xcode (~10-15 GB download). Free Apple ID is enough;
# no paid developer membership needed. xcodes handles the apple.com
# auth + .xip extraction headlessly.
brew install xcodes aria2
xcodes install 26.3.0     # or whatever version you want
sudo xcode-select -s /Applications/Xcode-26.3.0.app/Contents/Developer
sudo xcodebuild -license accept
sudo xcodebuild -runFirstLaunch

# Pull the Metal Toolchain component (~700 MB). MUST be run as each
# user that will compile shaders, not just root — even though the
# download is shared, the per-user MobileAsset registration is what
# wires up xcrun metal. If you skip this for `dev`, MLX will fail with
# `xcrun: error: unable to find utility "metal"`.
sudo xcodebuild -downloadComponent MetalToolchain
xcodebuild -downloadComponent MetalToolchain   # as dev

# Verify (must succeed for both users that will run MLX):
xcrun -f metal           # should print a /var/run/.../cryptexd/... path
xcrun metal --version    # should print "Apple metal version <ver>"
```

If `xcodebuild -downloadComponent MetalToolchain` fails with
`Failed fetching catalog`, retry — Apple's MobileAsset CDN is
intermittently flaky. Three or four attempts usually wins.

### Copying Xcode between macs

If you already have Xcode + Metal Toolchain working on one mac, the
fastest path to onboarding another is to rsync the Xcode.app over the
TB bridge (~3.7 GB at ~3 Gbps over `169.254.x.y`). The Metal Toolchain
itself lives in a SIP-protected cryptex (`/System/Library/AssetsV2/`)
and cannot be rsync'd — re-run `xcodebuild -downloadComponent` on the
target mac (as both root and dev) to pull it from Apple.

```bash
# from m1 (source), assuming SSH key set up to root@<m2-bridge-ip>:
rsync -aH /Applications/Xcode-26.3.0.app/ \
    root@<m2-tb-ip>:/Applications/Xcode-26.3.0.app/
# then on the target mac:
sudo xcode-select -s /Applications/Xcode-26.3.0.app/Contents/Developer
sudo xcodebuild -license accept
sudo xcodebuild -runFirstLaunch
sudo xcodebuild -downloadComponent MetalToolchain
xcodebuild -downloadComponent MetalToolchain   # as dev
```

## Remote (WireGuard-joined) nodes

A mac off the cluster LAN can join over WireGuard instead of the LAN bootstrap:

```bash
curl -sf https://<domain>/bootstrap/wg-macos | sudo bash -s -- --type macos
```

then approve it from a core node with `ycluster wg approve <hostname>`. This path
requires Homebrew preinstalled (it pulls `wireguard-tools`); the playbook then
adopts that brew install (normalizes ownership to `dev`). The node gets a
`10.0.1.x` wg address and is reachable cluster-side by name.

## Troubleshooting

Bootstrap logs: `/var/log/ycluster-bootstrap.log`. The script is idempotent and
can be re-run safely.

### Node flaps / intermittently unreachable (Apple Silicon)

Symptom: ICMP ping is rock-steady but SSH / node_exporter / wg intermittently
hang, and Prometheus `NodeDown` flaps roughly every ~15 min on a **headless**
mac.

Cause: macOS **"Maintenance Sleep"**. On Apple Silicon, `pmset sleep 0` only
disables the *idle* sleep timer — it does **not** prevent maintenance sleep. The
host sleeps in cycles; the NIC offload keeps answering pings (`tcpkeepalive`)
while userspace services hang, so the machine looks alive but isn't serving. A
Wake-on-LAN magic packet fully wakes it (hence "WoL fixed it").

Diagnose:
```bash
pmset -g log | grep -iE "Maintenance Sleep|DarkWake"   # cycles?
pmset -g | grep -i SleepDisabled                       # is disablesleep set?
# node_boot_time_seconds constant in Prometheus => not rebooting, just sleeping
```

Fix: `sudo pmset -a disablesleep 1` (now applied by the bootstrap and
`setup-macos.yml`). Nodes with a connected, never-sleeping display avoid this
incidentally — the display holds a "prevent sleep while display is on" assertion
— so a mac that was fine can start flapping if its monitor is unplugged.
`disablesleep` makes it robust regardless of any attached display.

### DNS (split DNS)

Cluster `.xc` names resolve via **`/etc/resolver/xc`** (→ cluster VIP, over wg
for remote nodes); everything else uses the node's own resolver (DHCP / Tailscale
/ ISP). Do **not** pin the whole Ethernet resolver to the cluster VIP
(`networksetup -setdnsservers`) — it breaks public DNS on a node with its own
networking. Note `getaddrinfo` / `dscacheutil` honor `/etc/resolver/`, but
`nslookup` / `dig` do **not** (they query the default resolver directly), so test
resolution with `dscacheutil -q host -a name <name>`, not `nslookup`. `jq` is
built into macOS since Sequoia (`/usr/bin/jq`), so the bootstrap no longer needs
it preinstalled.

### Tailscale coexistence

If a mac (or any node) uses a Tailscale **exit node**, Tailscale becomes the
**default route**, so the cluster wg tunnel's outer UDP egresses *through*
Tailscale — a tunnel inside a tunnel. That's an undesirable dependency (wg's
transport inherits Tailscale's exit-node MTU/availability), so prefer clearing
it on cluster nodes as hygiene.

Caveat — this was *not* shown to cause any symptom: the flapping that prompted
the investigation was maintenance sleep (fixed by `disablesleep`, proven by a
clean before/after), and clearing the exit node was not re-measured. Treat
exit-node removal as de-risking the architecture, not a diagnosed fix.

```bash
route -n get default | grep interface     # should be the physical NIC, NOT a utun
tailscale debug prefs | grep ExitNode      # ExitNodeID/IP should be empty
sudo tailscale set --exit-node=            # clear it (persists across reboots)
```
On macOS the App Store Tailscale CLI is at
`/Applications/Tailscale.app/Contents/MacOS/Tailscale` (not on `$PATH`).

### Reaching a remote mac when wg is down

Reach it over its LAN from a co-located cluster node, using the ansible key
(`/root/.ssh/id_ed25519` on the core nodes), jumping through the co-located node:
```bash
ssh <core> 'ssh -i /root/.ssh/id_ed25519 \
  -o ProxyCommand="ssh -W %h:%p <colocated-node>" \
  admin@<mac-lan-ip> "<cmd>"'
```
Find the mac's current LAN IP from the co-located node via its en0 MAC
(`ip neigh | grep -i <mac>`). To wake a sleeping mac, send a WoL magic packet
from the co-located node (L2 broadcast to the mac's en0 MAC; needs `womp 1`,
which is the default).
