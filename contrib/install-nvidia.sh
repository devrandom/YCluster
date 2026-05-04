#!/bin/bash
# Install NVIDIA open kernel modules, CUDA toolkit, and container toolkit on Ubuntu 24.04
set -euo pipefail

CUDA_VERSION="12-9"

apt-get update
apt-get install -y build-essential curl dkms linux-headers-$(uname -r) pkg-config libglvnd-dev python3-dev ninja-build

tmp=$(mktemp -d)
trap "rm -rf $tmp" EXIT

curl -fsSL https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb \
    -o "$tmp/cuda-keyring.deb"
dpkg -i "$tmp/cuda-keyring.deb"

apt-get update
apt-get install -y nvidia-open cuda-toolkit-${CUDA_VERSION} nvidia-container-toolkit

cat > /etc/profile.d/cuda.sh <<'EOF'
export PATH=/usr/local/cuda/bin:$PATH
export LD_LIBRARY_PATH=/usr/local/cuda/lib64:$LD_LIBRARY_PATH
EOF

systemctl enable --now nvidia-persistenced || true
systemctl restart docker || true

echo "Done. Reboot required to load NVIDIA kernel modules."
