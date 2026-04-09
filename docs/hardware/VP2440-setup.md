# VP2440 (s4) Manual Setup

Manual setup steps for the Protectli VP2440 as storage node s4, replacing the PXE autoinstall process.

## 1. Install Ubuntu 24.04

Install Ubuntu Server 24.04 LTS from USB. During install:
- Set hostname to `s4`
- Create `ubuntu` user with password
- Enable SSH

## 2. Disk Partitioning

The disk must use VG name `vg0` with a dedicated ceph LV. For a 2TB disk:

```
sda1  512M  EFI System Partition (vfat)
sda2  4G    /boot (ext4)
sda3  rest  LVM PV
  vg0/root  300G  / (ext4)
  vg0/ceph  ~1.5T (leave raw for MicroCeph)
```

If Ubuntu was installed with default partitioning (`ubuntu-vg`), redo it:

```bash
# From live USB or rescue mode:
vgrename ubuntu-vg vg0
lvrename vg0 ubuntu-lv root
# Update /etc/fstab and initramfs to reference new names
# Then create ceph LV from remaining space:
lvcreate -l 100%FREE -n ceph vg0
```

## 3. Post-Install Prerequisites

```bash
# SSH key for Ansible (root access)
mkdir -p /root/.ssh && chmod 700 /root/.ssh
curl -s http://10.0.0.254:8080/ssh-key >> /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys

# HWE kernel
apt install -y linux-generic-hwe-24.04

# MicroCeph
snap install microceph

# Reboot into HWE kernel
reboot
```

## 4. Network

Connect an SFP+ cable to port `enp1s0f0np0` (first X710 port). Set a temporary static IP so Ansible can reach it:

```bash
ip addr add 10.0.0.14/24 dev enp1s0f0np0
ip link set enp1s0f0np0 up
```

Ansible will deploy the final netplan config.

## 5. Register in etcd

From any etcd node (s1-s3):

```bash
etcdctl put /cluster/nodes/by-hostname/s4 \
  '{"hostname":"s4","ip":"10.0.0.14","type":"storage","mac":"64:62:66:25:38:dd"}'
```

## 6. Join MicroCeph

```bash
# On an existing storage node:
microceph cluster add s4

# On s4, with the token from above:
microceph cluster join <token>
```

## 7. Run Ansible

```bash
ssh s3.yc "cd /etc/ansible && ./run-playbook.sh site.yml --limit s4"
```

The ceph disk (`/dev/mapper/vg0-ceph`) will be added automatically by the storage playbook.

## Notes

- s4 does not run etcd. It uses remote etcd endpoints (s1-s3) for leader election and cluster state.
- s4 can be promoted to an etcd member later if needed (e.g., to replace a failed node).
- See `docs/hardware/VP2440.md` for hardware details and NIC mapping.
