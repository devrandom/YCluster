# Dev PXE environment: dnsmasq (DHCP+DNS+TFTP) + tftpboot extracted from
# the Ubuntu live-server ISO. Used to test autoinstall locally without
# touching prod.
#
# just has no Make-style file targets, so the one-shot file-creation
# recipes below guard each body with `if [ ! -e FILE ]` and only run
# when the target is missing. Cheap idempotent operations (touch) could
# always run, but the expensive ones (wget, xorriso, apt-get) get the
# guard.

# Pull the ISO + scratch files. Once-off bootstrap for fresh checkouts.
init: _fetch-iso _init-data-leases _init-ansible-ssh-key

# Dev setup - prepares tftpboot directory (does NOT start dnsmasq)
dev-setup: dev-tftpboot _init-dev-leases
    @echo "Dev environment ready. Run 'just dev-dnsmasq-start' to start DHCP/DNS/TFTP."

# Prepare TFTP boot directory from Ubuntu ISO
dev-tftpboot: _extract-vmlinuz _extract-grub _copy-grub-cfg

# Auto-detected interface with 10.0.0.254; override with DEV_INTERFACE.
# Re-runs if the template is newer than the rendered config.
[private]
_dnsmasq-conf:
    @DEV_INTERFACE="${DEV_INTERFACE:-$(ip -o addr show | grep 'inet 10\.0\.0\.254/' | awk '{print $2}')}"
    @if [ -z "$DEV_INTERFACE" ]; then echo "Error: No interface with 10.0.0.254 found. Set DEV_INTERFACE manually."; exit 1; fi
    @if [ ! -e ./dev/dnsmasq.conf ] || [ ./dev/dnsmasq.conf.template -nt ./dev/dnsmasq.conf ]; then \
        PROJECT_ROOT="$(pwd)" DEV_INTERFACE="$DEV_INTERFACE" envsubst < ./dev/dnsmasq.conf.template > ./dev/dnsmasq.conf && \
        echo "Generated dev/dnsmasq.conf (interface: $DEV_INTERFACE)"; \
    fi

# Full dev host setup: rootless Incus, Go toolchain, Python tooling.
setup: setup-rootless-incus setup-go setup-tools

# Host-level Go toolchain + pinned quality-metric binaries.
setup-go:
    @if ! command -v go >/dev/null 2>&1; then sudo apt-get update && sudo apt-get install -y golang-go; fi
    @just metrics-tools

# Privileged: start dnsmasq in foreground with debug (sudo drops to 'dev' after binding)
dev-dnsmasq-start: _dnsmasq-conf
    sudo dnsmasq -d --log-debug -C "$(pwd)/dev/dnsmasq.conf" --user=dev --group=dev

# Privileged: stop dnsmasq
dev-dnsmasq-stop:
    sudo pkill -f "dnsmasq -C $(pwd)/dev/dnsmasq.conf" || true

# Privileged: watch for changes and auto-restart dnsmasq
dev-dnsmasq-watch: dev-tftpboot _init-dev-leases
    sudo ./dev/dnsmasq-watch.sh

# ---- one-shot file-creation helpers (private) -------------------------

[private]
_init-dev-leases:
    @if [ ! -e ./dev/dnsmasq.leases ]; then mkdir -p ./dev && touch ./dev/dnsmasq.leases; fi

[private]
_extract-vmlinuz: _fetch-iso
    @if [ ! -e ./dev/tftpboot/ubuntu/vmlinuz ]; then \
        mkdir -p ./dev/tftpboot/ubuntu && \
        xorriso -osirrox on -indev ./data/ubuntu-24.04.3-live-server-amd64.iso \
            -extract /casper/hwe-vmlinuz ./dev/tftpboot/ubuntu/vmlinuz \
            -extract /casper/hwe-initrd ./dev/tftpboot/ubuntu/initrd && \
        ln -f ./data/ubuntu-24.04.3-live-server-amd64.iso ./dev/tftpboot/ubuntu/ 2>/dev/null || true; \
    fi

[private]
_extract-grub:
    @if [ ! -e ./dev/tftpboot/EFI/BOOT/grubx64.efi ]; then \
        mkdir -p ./dev/tftpboot/EFI/BOOT && \
        cd ./dev/tftpboot/EFI/BOOT && \
        apt-get download shim-signed grub-efi-amd64-signed && \
        dpkg-deb --fsys-tarfile shim-signed*deb | tar x ./usr/lib/shim/shimx64.efi.signed.latest -O > bootx64.efi && \
        dpkg-deb --fsys-tarfile grub-efi-amd64-signed*deb | tar x ./usr/lib/grub/x86_64-efi-signed/grubnetx64.efi.signed -O > grubx64.efi && \
        rm -f *.deb; \
    fi

[private]
_copy-grub-cfg:
    @if [ ! -e ./dev/tftpboot/grub/grub.cfg ] || [ ./dev/grub.cfg -nt ./dev/tftpboot/grub/grub.cfg ]; then \
        mkdir -p ./dev/tftpboot/grub && \
        cp ./dev/grub.cfg ./dev/tftpboot/grub/; \
    fi

[private]
_fetch-checksums:
    @if [ ! -e ./data/SHA256SUMS ]; then wget -O ./data/SHA256SUMS https://releases.ubuntu.com/24.04.3/SHA256SUMS; fi

[private]
_fetch-iso: _fetch-checksums
    @if [ ! -e ./data/ubuntu-24.04.3-live-server-amd64.iso ]; then \
        wget -O ./data/ubuntu-24.04.3-live-server-amd64.iso https://releases.ubuntu.com/24.04.3/ubuntu-24.04.3-live-server-amd64.iso && \
        cd ./data && sha256sum -c --ignore-missing SHA256SUMS; \
    fi

[private]
_init-data-leases:
    @if [ ! -e ./data/dnsmasq.leases ]; then touch ./data/dnsmasq.leases; fi

[private]
_init-ansible-ssh-key:
    @if [ ! -e ./data/ansible_ssh_key ]; then ssh-keygen -t ed25519 -f ./data/ansible_ssh_key -N "" -C "ansible@pxe-server"; fi
