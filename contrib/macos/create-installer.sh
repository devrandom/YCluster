#!/bin/bash
set -euo pipefail

# Create a bootable macOS installer on a USB drive

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Create a bootable macOS installer on a USB drive.

Options:
    -d, --disk DISK         Target disk (e.g., disk4)
    -p, --package FILE      Bootstrap package (default: bootstrap.pkg)
    -v, --version VERSION   macOS version to download (e.g., 14.5)
    -i, --installer PATH    Path to existing installer app
    -l, --list              List disks and installers (default if no --disk)
    -h, --help              Show this help

By default (no args), lists available disks and installers.
Uses highest version installer in /Applications, or downloads latest.

Examples:
    $(basename "$0")                            # List disks and installers
    $(basename "$0") --disk disk4               # Create installer (uses bootstrap.pkg)
    $(basename "$0") --disk disk4 --version 14.5
EOF
    exit "${1:-1}"
}

list_info() {
    echo "=== External Disks ==="
    diskutil list external
    echo
    echo "=== macOS Installers (current directory) ==="
    ls -d ./Install\ macOS*.app 2>/dev/null || echo "  None found"
    echo
    echo "=== macOS Installers (/Applications) ==="
    ls -d /Applications/Install\ macOS*.app 2>/dev/null || echo "  None found"
    echo
    echo "=== Available for Download ==="
    softwareupdate --list-full-installers
}

# Get the highest version installer
# Searches current directory first, then /Applications
# Reads version from Info.plist and sorts numerically
get_highest_version_installer() {
    local highest_version=""
    local highest_installer=""

    for dir in "." "/Applications"; do
        for app in "$dir"/Install\ macOS*.app; do
            [[ -d "$app" ]] || continue
            local plist="$app/Contents/Info.plist"
            if [[ -f "$plist" ]]; then
                local version
                version=$(/usr/libexec/PlistBuddy -c "Print :DTPlatformVersion" "$plist" 2>/dev/null || echo "0")
                if [[ -z "$highest_version" ]] || [[ "$(printf '%s\n' "$version" "$highest_version" | sort -V | tail -1)" == "$version" ]]; then
                    highest_version="$version"
                    highest_installer="$app"
                fi
            fi
        done
    done

    echo "$highest_installer"
}

# Get the highest version available for download
get_highest_download_version() {
    softwareupdate --list-full-installers 2>/dev/null | \
        grep -oE 'Version: [0-9]+\.[0-9]+(\.[0-9]+)?' | \
        sed 's/Version: //' | \
        sort -V | \
        tail -1
}

DISK=""
VERSION=""
INSTALLER=""
PACKAGE="bootstrap.pkg"

while [[ $# -gt 0 ]]; do
    case $1 in
        -l|--list)
            list_info
            exit 0
            ;;
        -d|--disk)
            DISK="$2"
            shift 2
            ;;
        -v|--version)
            VERSION="$2"
            shift 2
            ;;
        -i|--installer)
            INSTALLER="$2"
            shift 2
            ;;
        -p|--package)
            PACKAGE="$2"
            shift 2
            ;;
        -h|--help)
            usage 0
            ;;
        *)
            echo "Unknown option: $1"
            usage
            ;;
    esac
done

# Default to listing info if no disk specified
if [[ -z "$DISK" ]]; then
    list_info
    echo
    usage 0
fi

# Require bootstrap package
if [[ ! -f "$PACKAGE" ]]; then
    echo "Error: Package not found: $PACKAGE"
    echo "Create one with: ./create-bootstrap.sh --key ~/.ssh/id_ed25519.pub"
    exit 1
fi

# Validate disk exists and is external/removable
DISK_INFO=$(diskutil info -plist "$DISK" 2>/dev/null) || {
    echo "Error: Disk $DISK not found"
    exit 1
}

DEVICE_LOCATION=$(echo "$DISK_INFO" | plutil -extract DeviceLocation raw -o - - 2>/dev/null || echo "")
REMOVABLE=$(echo "$DISK_INFO" | plutil -extract RemovableMedia raw -o - - 2>/dev/null || echo "")

if [[ "$DEVICE_LOCATION" != "External" && "$REMOVABLE" != "true" ]]; then
    echo "Error: $DISK does not appear to be an external/removable disk"
    echo "  DeviceLocation: $DEVICE_LOCATION"
    echo "  RemovableMedia: $REMOVABLE"
    echo "Refusing to proceed for safety"
    exit 1
fi

# Download installer if version specified
if [[ -n "$VERSION" ]]; then
    echo "Downloading macOS $VERSION installer..."
    softwareupdate --fetch-full-installer --full-installer-version "$VERSION"

    # Find the downloaded installer
    INSTALLER=$(ls -dt /Applications/Install\ macOS*.app 2>/dev/null | head -1)
    if [[ -z "$INSTALLER" ]]; then
        echo "Error: Could not find downloaded installer"
        exit 1
    fi
    echo "Using installer: $INSTALLER"
fi

if [[ -z "$INSTALLER" ]]; then
    # Try to find the highest version installed installer
    INSTALLER=$(get_highest_version_installer)
    if [[ -z "$INSTALLER" ]]; then
        # No installer found, download the latest
        echo "No installer found. Fetching latest version..."
        VERSION=$(get_highest_download_version)
        if [[ -z "$VERSION" ]]; then
            echo "Error: Could not determine latest macOS version"
            exit 1
        fi
        echo "Downloading macOS $VERSION..."
        softwareupdate --fetch-full-installer --full-installer-version "$VERSION"
        INSTALLER=$(get_highest_version_installer)
        if [[ -z "$INSTALLER" ]]; then
            echo "Error: Could not find downloaded installer"
            exit 1
        fi
    fi
    echo "Using installer: $INSTALLER"
fi

if [[ ! -d "$INSTALLER" ]]; then
    echo "Error: Installer not found: $INSTALLER"
    exit 1
fi

# Extract macOS name from installer path for volume naming
MACOS_NAME=$(basename "$INSTALLER" | sed 's/Install macOS //' | sed 's/\.app//')

echo
echo "=== Configuration ==="
echo "Disk:      /dev/$DISK"
echo "Installer: $INSTALLER"
echo "Package:   ${PACKAGE:-none}"
echo

# Confirm
read -p "This will ERASE /dev/$DISK. Continue? [y/N] " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted"
    exit 1
fi

# Format the disk
echo "Formatting /dev/$DISK..."
diskutil unmountDisk force "/dev/$DISK" 2>/dev/null || true
diskutil eraseDisk JHFS+ "Installer" GPT "/dev/$DISK"

# Wait for volume to mount
sleep 2

# Disable journaling so Linux can mount read-write
echo "Disabling journaling for Linux compatibility..."
diskutil disableJournal /Volumes/Installer 2>/dev/null || true

# Find the volume
VOLUME=$(diskutil list "$DISK" | grep "Installer" | awk '{print $NF}')
if [[ -z "$VOLUME" ]]; then
    echo "Error: Could not find formatted volume"
    exit 1
fi

echo "Creating bootable installer on /Volumes/Installer..."
sudo "$INSTALLER/Contents/Resources/createinstallmedia" \
    --volume /Volumes/Installer \
    --nointeraction

# Find the new volume name (createinstallmedia renames it)
sleep 2
INSTALL_VOLUME=$(ls -d /Volumes/Install\ macOS* 2>/dev/null | head -1)

if [[ -z "$INSTALL_VOLUME" ]]; then
    echo "Warning: Could not find installer volume after creation"
else
    echo "Installer created at: $INSTALL_VOLUME"

    # Copy target scripts (install.sh, etc.)
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    if [[ -d "$SCRIPT_DIR/target" ]]; then
        echo "Copying target scripts..."
        cp "$SCRIPT_DIR/target/"* "$INSTALL_VOLUME/"
        chmod +x "$INSTALL_VOLUME/"*.sh 2>/dev/null || true
    fi

    # Copy bootstrap package
    echo "Copying bootstrap package..."
    cp "$PACKAGE" "$INSTALL_VOLUME/"
    echo "Package copied to: $INSTALL_VOLUME/$(basename "$PACKAGE")"
fi

echo
echo "Done! On target Mac:"
echo "  1. Hold power button until 'Loading startup options' appears"
echo "  2. Select the installer USB"
echo "  3. Choose 'Install macOS' and follow prompts"
echo "  4. Bootstrap package runs automatically during install"
