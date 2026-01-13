# macOS Provisioning

Scripts for provisioning Mac compute nodes in the cluster.

## Overview

Unlike Linux nodes which use PXE boot, Macs are provisioned using a bootable USB installer with a bootstrap package that configures the system on first boot.

## Quick Start

```bash
# 1. Create bootstrap package with your SSH key
./create-bootstrap.sh --key ~/.ssh/id_ed25519.pub

# 2. Create bootable USB installer (plug in USB drive first)
./create-installer.sh --disk disk4
```

## Scripts

| Script | Platform | Description |
|--------|----------|-------------|
| `create-bootstrap.sh` | macOS/Linux | Builds bootstrap.pkg for post-install configuration |
| `create-installer.sh` | macOS | Creates bootable USB installer |
| `target/install.sh` | Target Mac | Runs startosinstall (copied to USB automatically) |

## Workflow

### 1. Create Bootstrap Package

```bash
# With SSH key (recommended - disables password auth)
./create-bootstrap.sh --key ~/.ssh/id_ed25519.pub

# With explicit password and hostname
./create-bootstrap.sh --pass secret123 --hostname mac-mini-1

# With custom username
./create-bootstrap.sh --user myadmin --key ~/.ssh/id_ed25519.pub

# On Linux (requires xar)
# Fedora:
dnf install xar

# Ubuntu/Debian (build from source):
apt install libxml2-dev libssl-dev zlib1g-dev build-essential autoconf
git clone https://github.com/mackyle/xar.git && cd xar/xar
./autogen.sh && ./configure && make && sudo make install

./create-bootstrap.sh --key ~/.ssh/id_ed25519.pub
```

The bootstrap package configures:
- Admin user with sudo access
- SSH public key in `~/.ssh/authorized_keys`
- Remote Login (SSH) enabled
- Password authentication disabled (if SSH key provided)
- Hostname (optional)

### 2. Create Bootable USB Installer

```bash
# List available disks and installers
./create-installer.sh

# Create installer on disk4 using highest version macOS
./create-installer.sh --disk disk4

# Download and use specific macOS version
./create-installer.sh --disk disk4 --version 14.5
```

The script:
- Validates the disk is external/removable (safety check)
- Uses highest version installer from current directory or /Applications
- Downloads latest macOS if no installer found
- Copies `target/install.sh` and `bootstrap.pkg` to the USB

### 3. Install macOS on Target Mac (Apple Silicon)

1. Boot target Mac from USB:
   - Hold power button until "Loading startup options" appears
   - Select the installer USB

2. Select "Install macOS" from the Recovery menu

3. Follow the prompts to select your disk

4. The bootstrap package is installed automatically during setup

After installation completes and the Mac reboots, SSH should be available.

## Testing

Test the bootstrap package on an existing Mac:

```bash
# Inspect package contents
pkgutil --expand bootstrap.pkg /tmp/bootstrap-test
cat /tmp/bootstrap-test/Scripts/postinstall

# Install (runs postinstall script as root)
sudo installer -pkg bootstrap.pkg -target /

# Check logs
cat /var/log/ycluster-bootstrap.log

# Clean up test user
sudo sysadminctl -deleteUser testadmin
```

## Directory Structure

```
contrib/macos/
├── create-bootstrap.sh      # Build bootstrap.pkg (macOS/Linux)
├── create-installer.sh      # Create bootable USB (macOS only)
├── bootstrap.pkg            # Generated package (gitignored)
├── bootstrap/
│   └── scripts/
│       └── postinstall      # Template script baked into pkg
└── target/
    └── install.sh           # Copied to USB, runs on target Mac
```

## Troubleshooting

### Disk not appearing in macOS

Check system logs:
```bash
log stream --predicate 'subsystem == "com.apple.diskarbitration"'
```

If loginwindow is ejecting the disk, check:
**System Settings → Privacy & Security → Allow accessories to connect**

Set to "Always" instead of "When Unlocked".

### USB formatting fails

Some large or unusual USB drives have issues with GPT/HFS+. The script uses:
```bash
diskutil eraseDisk JHFS+ "Installer" GPT "/dev/$DISK"
```

If this fails, try formatting manually with Disk Utility first.

## Limitations

- **No zero-touch install**: USB boot requires physical presence to select the installer and click through the GUI
- **No custom boot menu entries**: Cannot add custom icons to the recovery boot picker
- **Apple Silicon only**: This workflow targets Apple Silicon Macs (M1/M2/M3/M4)

For fully automated Mac provisioning, consider MDM (Jamf, Mosyle, MicroMDM).
