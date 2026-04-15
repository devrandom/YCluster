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

## Troubleshooting

Bootstrap logs: `/var/log/ycluster-bootstrap.log`

The script is idempotent and can be re-run safely.
