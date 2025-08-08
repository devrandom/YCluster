#!/bin/bash
set -e

# Create TFTP boot directory
BOOT=/var/lib/tftpboot
mkdir -p $BOOT
mkdir -p $BOOT/ubuntu
mkdir -p $BOOT/EFI/BOOT
mkdir -p $BOOT/grub

# Copy the extracted EFI files to TFTP directory
cp /root/*.efi $BOOT/EFI/BOOT
cp /root/grub.cfg $BOOT/grub/grub.cfg

# Find Ubuntu ISO file in /data directory
ISO_FILE=$(find /data -name "ubuntu-*.iso" | head -1)

if [ -n "$ISO_FILE" ] && [ -f "$ISO_FILE" ]; then
    echo "Found Ubuntu ISO: $ISO_FILE"
    isoinfo -R -x /casper/vmlinuz -i $ISO_FILE > $BOOT/ubuntu/vmlinuz
    isoinfo -R -x /casper/initrd -i $ISO_FILE > $BOOT/ubuntu/initrd
    cp "$ISO_FILE" $BOOT/ubuntu/
else
    echo "Error: No Ubuntu ISO file found in /data directory"
    exit 1
fi

# List contents of TFTP directory
echo "TFTP boot directory contents:"
ls -la $BOOT

# Create directory for additional configs
mkdir -p /etc/dnsmasq.d

# Create empty static hosts file (not needed for static IP setup but keeps config clean)
touch /etc/dnsmasq.d/static-hosts.conf
echo "Created empty static hosts file"

# Start dnsmasq without dhcp-script since we use static IP assignment
exec /usr/sbin/dnsmasq \
    --conf-dir=/etc/dnsmasq.d \
    --dhcp-authoritative \
    --dhcp-match=set:efibc,option:client-arch,7 \
    --dhcp-boot=tag:efibc,EFI/BOOT/grubx64.efi \
    --dhcp-option=42,10.0.0.1 \
    --listen-address=10.0.0.1 \
    --bind-interfaces \
    --dhcp-range=10.0.0.200,10.0.0.249,12h \
    --enable-tftp \
    --tftp-root=/var/lib/tftpboot \
    --local=/xc/ \
    --domain=xc \
    --domain-needed \
    -d \
    --log-dhcp \
    --log-queries \
    --log-facility=- \
    "$@"
