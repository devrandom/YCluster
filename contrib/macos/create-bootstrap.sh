#!/bin/bash
set -euo pipefail

# Build a bootstrap package for macOS provisioning
# Works on both macOS and Linux

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOOTSTRAP_DIR="$SCRIPT_DIR/bootstrap"
SCRIPTS_DIR="$BOOTSTRAP_DIR/scripts"

# Detect platform
OS="$(uname -s)"

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Build a bootstrap.pkg for macOS provisioning.

Options:
    -u, --user NAME         Admin username (default: admin)
    -p, --pass PASSWORD     Password for admin user (default: random)
    -k, --key FILE          SSH public key file (e.g., ~/.ssh/id_ed25519.pub)
    -K, --key-string KEY    SSH public key as string
    -n, --hostname NAME     Hostname to set (optional)
    -o, --output FILE       Output package path (default: ./bootstrap.pkg)
    -h, --help              Show this help

Examples:
    $(basename "$0") --key ~/.ssh/id_ed25519.pub
    $(basename "$0") --pass secret123 --hostname mac-compute-1
    $(basename "$0") --user myadmin --key ~/.ssh/id_ed25519.pub
EOF
    exit "${1:-1}"
}

ADMIN_USER="admin"
ADMIN_PASS=""
SSH_KEY=""
HOSTNAME=""
OUTPUT="./bootstrap.pkg"

while [[ $# -gt 0 ]]; do
    case $1 in
        -u|--user)
            ADMIN_USER="$2"
            shift 2
            ;;
        -p|--pass)
            ADMIN_PASS="$2"
            shift 2
            ;;
        -k|--key)
            SSH_KEY=$(cat "$2")
            shift 2
            ;;
        -K|--key-string)
            SSH_KEY="$2"
            shift 2
            ;;
        -n|--hostname)
            HOSTNAME="$2"
            shift 2
            ;;
        -o|--output)
            OUTPUT="$2"
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

# Generate random password if not provided
if [[ -z "$ADMIN_PASS" ]]; then
    ADMIN_PASS=$(openssl rand -base64 16 | tr -dc 'a-zA-Z0-9' | head -c 16)
    echo "Generated password: $ADMIN_PASS"
fi

# Create temp directory for package building
TEMP_DIR=$(mktemp -d)
trap "rm -rf $TEMP_DIR" EXIT

# Generate postinstall script from template
POSTINSTALL="$TEMP_DIR/scripts/postinstall"
mkdir -p "$TEMP_DIR/scripts"

awk \
    -v admin_user="$ADMIN_USER" \
    -v admin_pass="$ADMIN_PASS" \
    -v ssh_key="$SSH_KEY" \
    -v hostname="$HOSTNAME" \
    '{
        gsub(/__ADMIN_USER__/, admin_user)
        gsub(/__ADMIN_PASS__/, admin_pass)
        gsub(/__SSH_KEY__/, ssh_key)
        gsub(/__HOSTNAME__/, hostname)
        print
    }' "$SCRIPTS_DIR/postinstall" > "$POSTINSTALL"

chmod +x "$POSTINSTALL"

# Build the package
echo "Building bootstrap package..."

if [[ "$OS" == "Darwin" ]]; then
    # macOS: use pkgbuild
    pkgbuild \
        --nopayload \
        --scripts "$TEMP_DIR/scripts" \
        --identifier com.ycluster.bootstrap \
        --version 1.0 \
        "$OUTPUT"
else
    # Linux: build flat package manually with xar and cpio
    if ! command -v xar &>/dev/null; then
        echo "Error: xar is required on Linux."
        echo
        echo "Install options:"
        echo "  # Ubuntu/Debian (build from source)"
        echo "  apt install libxml2-dev libssl-dev zlib1g-dev build-essential"
        echo "  git clone https://github.com/mackyle/xar.git && cd xar/xar"
        echo "  ./autogen.sh && ./configure && make && sudo make install"
        echo
        echo "  # Fedora"
        echo "  dnf install xar"
        echo
        echo "  # Or run this script on macOS instead"
        exit 1
    fi

    PKG_DIR="$TEMP_DIR/pkg"
    mkdir -p "$PKG_DIR"

    # Create Scripts archive (cpio + gzip)
    (cd "$TEMP_DIR/scripts" && find . -print | cpio -o --format odc 2>/dev/null | gzip -c > "$PKG_DIR/Scripts")

    # Calculate installed size (scripts only, in KB)
    SCRIPTS_SIZE=$(du -sk "$TEMP_DIR/scripts" | cut -f1)

    # Create PackageInfo
    cat > "$PKG_DIR/PackageInfo" <<PKGINFO
<?xml version="1.0" encoding="utf-8"?>
<pkg-info format-version="2" identifier="com.ycluster.bootstrap" version="1.0" install-location="/" auth="root">
    <scripts>
        <postinstall file="./postinstall"/>
    </scripts>
</pkg-info>
PKGINFO

    # Create the xar archive
    (cd "$PKG_DIR" && xar -cf "$OLDPWD/$OUTPUT" --compression=gzip PackageInfo Scripts)
fi

echo
echo "=== Package created: $OUTPUT ==="
echo "Admin user: $ADMIN_USER"
echo "Password:   $ADMIN_PASS"
if [[ -n "$SSH_KEY" ]]; then
    echo "SSH key:    installed"
fi
if [[ -n "$HOSTNAME" ]]; then
    echo "Hostname:   $HOSTNAME"
fi
echo
echo "Use with create-installer.sh:"
echo "  ./create-installer.sh --disk diskN --package $OUTPUT"
