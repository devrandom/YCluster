.PHONY: init dev-setup dev-dnsmasq-start dev-dnsmasq-stop dev-dnsmasq-watch dev-tftpboot

init: ./data/ubuntu-24.04.3-live-server-amd64.iso ./data/dnsmasq.leases ./data/ansible_ssh_key

# Dev setup - prepares tftpboot directory and runs dnsmasq
dev-setup: dev-tftpboot ./dev/dnsmasq.leases
	@echo "Dev environment ready. Run 'make dev-dnsmasq' to start DHCP/DNS/TFTP."

# Generate dnsmasq.conf from template
# Auto-detects interface with 10.0.0.254, or set DEV_INTERFACE to override
./dev/dnsmasq.conf: dev/dnsmasq.conf.template
	$(eval DEV_INTERFACE ?= $(shell ip -o addr show | grep 'inet 10\.0\.0\.254/' | awk '{print $$2}'))
	@test -n "$(DEV_INTERFACE)" || { echo "Error: No interface with 10.0.0.254 found. Set DEV_INTERFACE manually."; exit 1; }
	PROJECT_ROOT=$$(pwd) DEV_INTERFACE=$(DEV_INTERFACE) envsubst < dev/dnsmasq.conf.template > ./dev/dnsmasq.conf
	@echo "Generated dev/dnsmasq.conf (interface: $(DEV_INTERFACE))"

# Privileged: start dnsmasq in foreground with debug (run as user with sudo, drops to 'dev' after binding)
dev-dnsmasq-start: ./dev/dnsmasq.conf
	sudo dnsmasq -d --log-debug -C $$(pwd)/dev/dnsmasq.conf --user=dev --group=dev

# Privileged: stop dnsmasq
dev-dnsmasq-stop:
	sudo pkill -f "dnsmasq -C $$(pwd)/dev/dnsmasq.conf" || true

# Privileged: watch for changes and auto-restart dnsmasq
dev-dnsmasq-watch: dev-tftpboot ./dev/dnsmasq.leases
	sudo ./dev/dnsmasq-watch.sh

# Create dev leases file
./dev/dnsmasq.leases:
	mkdir -p ./dev
	touch ./dev/dnsmasq.leases

# Prepare TFTP boot directory from Ubuntu ISO
dev-tftpboot: ./dev/tftpboot/ubuntu/vmlinuz ./dev/tftpboot/EFI/BOOT/grubx64.efi ./dev/tftpboot/grub/grub.cfg
	@echo "TFTP boot directory ready at ./dev/tftpboot"

./dev/tftpboot/ubuntu/vmlinuz: ./data/ubuntu-24.04.3-live-server-amd64.iso
	mkdir -p ./dev/tftpboot/ubuntu
	xorriso -osirrox on -indev ./data/ubuntu-24.04.3-live-server-amd64.iso \
		-extract /casper/hwe-vmlinuz ./dev/tftpboot/ubuntu/vmlinuz \
		-extract /casper/hwe-initrd ./dev/tftpboot/ubuntu/initrd
	ln ./data/ubuntu-24.04.3-live-server-amd64.iso ./dev/tftpboot/ubuntu/ 2>/dev/null || cp ./data/ubuntu-24.04.3-live-server-amd64.iso ./dev/tftpboot/ubuntu/

./dev/tftpboot/EFI/BOOT/grubx64.efi:
	mkdir -p ./dev/tftpboot/EFI/BOOT
	cd ./dev/tftpboot/EFI/BOOT && \
		apt-get download shim-signed grub-efi-amd64-signed && \
		dpkg-deb --fsys-tarfile shim-signed*deb | tar x ./usr/lib/shim/shimx64.efi.signed.latest -O > bootx64.efi && \
		dpkg-deb --fsys-tarfile grub-efi-amd64-signed*deb | tar x ./usr/lib/grub/x86_64-efi-signed/grubnetx64.efi.signed -O > grubx64.efi && \
		rm -f *.deb

./dev/tftpboot/grub/grub.cfg: dev/grub.cfg
	mkdir -p ./dev/tftpboot/grub
	cp dev/grub.cfg ./dev/tftpboot/grub/


./data/SHA256SUMS:
	wget -O ./data/SHA256SUMS https://releases.ubuntu.com/24.04.3/SHA256SUMS

./data/ubuntu-24.04.3-live-server-amd64.iso: ./data/SHA256SUMS
	wget -O ./data/ubuntu-24.04.3-live-server-amd64.iso https://releases.ubuntu.com/24.04.3/ubuntu-24.04.3-live-server-amd64.iso
	cd ./data && sha256sum -c --ignore-missing SHA256SUMS

./data/dnsmasq.leases:
	touch ./data/dnsmasq.leases

./data/ansible_ssh_key:
	ssh-keygen -t ed25519 -f ./data/ansible_ssh_key -N "" -C "ansible@pxe-server"
