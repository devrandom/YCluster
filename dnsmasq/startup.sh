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

# Start dnsmasq with the original arguments
exec /usr/sbin/dnsmasq \
    --dhcp-authoritative \
    --dhcp-match=set:efibc,option:client-arch,7 \
    --dhcp-boot=tag:efibc,EFI/BOOT/grubx64.efi \
    --dhcp-option=42,10.0.0.1 \
    --listen-address=10.0.0.1 \
    --bind-interfaces \
    --dhcp-range=10.0.0.100,10.0.0.200 \
    --enable-tftp \
    --tftp-root=/var/lib/tftpboot \
    -d \
    --log-dhcp \
    --log-queries \
    --log-facility=- \
    "$@"
