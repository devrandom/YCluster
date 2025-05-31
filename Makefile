.PHONY: init

init: ./data/ubuntu-24.04.2-live-server-amd64.iso ./data/dnsmasq.leases ./data/ansible_ssh_key


./data/SHA256SUMS:
	wget -O ./data/SHA256SUMS https://releases.ubuntu.com/24.04.2/SHA256SUMS

./data/ubuntu-24.04.2-live-server-amd64.iso: ./data/SHA256SUMS
	wget -O ./data/ubuntu-24.04.2-live-server-amd64.iso https://releases.ubuntu.com/24.04.2/ubuntu-24.04.2-live-server-amd64.iso
	cd ./data && sha256sum -c --ignore-missing SHA256SUMS

./data/dnsmasq.leases:
	touch ./data/dnsmasq.leases

./data/ansible_ssh_key:
	ssh-keygen -t ed25519 -f ./data/ansible_ssh_key -N "" -C "ansible@pxe-server"
