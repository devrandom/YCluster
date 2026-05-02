"""
Hardware inventory and asset management utilities.

etcd schema:
  /cluster/nodes/hardware/<hostname>  -> hardware facts JSON (auto-collected)
  /cluster/nodes/asset/<hostname>     -> asset metadata JSON (manually entered)

Hardware JSON fields (collected by Ansible / ycluster inventory collect):
  cpu, ram_gb, disks, nics, gpus, serial, product, bios_version,
  os, kernel, collected_at

Asset JSON fields (entered via UI or CLI):
  vendor, purchased_at, warranty_expires, cost, cost_currency, location, notes,
  updated_at
"""

import json
import subprocess
import platform
from datetime import datetime, UTC

from ..common.etcd_utils import get_etcd_client

HARDWARE_PREFIX = '/cluster/nodes/hardware/'
ASSET_PREFIX = '/cluster/nodes/asset/'
NODE_PREFIX = '/cluster/nodes/by-hostname/'


# ---------------------------------------------------------------------------
# etcd helpers
# ---------------------------------------------------------------------------

def get_hardware(hostname):
    """Return hardware facts dict for hostname, or None."""
    client = get_etcd_client()
    result = client.get(f'{HARDWARE_PREFIX}{hostname}')
    if result[0] is None:
        return None
    return json.loads(result[0].decode())


def put_hardware(hostname, data):
    """Persist hardware facts for hostname."""
    client = get_etcd_client()
    client.put(f'{HARDWARE_PREFIX}{hostname}', json.dumps(data))


def get_asset(hostname):
    """Return asset metadata dict for hostname, or {}."""
    client = get_etcd_client()
    result = client.get(f'{ASSET_PREFIX}{hostname}')
    if result[0] is None:
        return {}
    return json.loads(result[0].decode())


def put_asset(hostname, data):
    """Persist asset metadata for hostname. Merges with existing data."""
    existing = get_asset(hostname)
    existing.update(data)
    existing['updated_at'] = datetime.now(UTC).isoformat()
    client = get_etcd_client()
    client.put(f'{ASSET_PREFIX}{hostname}', json.dumps(existing))
    return existing


def list_all():
    """
    Return list of dicts, one per known node, combining allocation +
    hardware + asset data. Nodes with no hardware data still appear.
    """
    client = get_etcd_client()

    # Gather all node allocations
    nodes = {}
    for value, meta in client.get_prefix(NODE_PREFIX):
        try:
            alloc = json.loads(value.decode())
            hostname = alloc.get('hostname')
            if hostname and not hostname.startswith('dhcp-'):
                nodes[hostname] = {'allocation': alloc, 'hardware': None, 'asset': {}}
        except Exception:
            pass

    # Overlay hardware facts
    for value, meta in client.get_prefix(HARDWARE_PREFIX):
        try:
            hw = json.loads(value.decode())
            hostname = meta.key.decode().removeprefix(HARDWARE_PREFIX)
            if hostname in nodes:
                nodes[hostname]['hardware'] = hw
            else:
                nodes[hostname] = {'allocation': None, 'hardware': hw, 'asset': {}}
        except Exception:
            pass

    # Overlay asset metadata
    for value, meta in client.get_prefix(ASSET_PREFIX):
        try:
            asset = json.loads(value.decode())
            hostname = meta.key.decode().removeprefix(ASSET_PREFIX)
            if hostname in nodes:
                nodes[hostname]['asset'] = asset
            else:
                nodes[hostname] = {'allocation': None, 'hardware': None, 'asset': asset}
        except Exception:
            pass

    return list(nodes.values())


# ---------------------------------------------------------------------------
# Hardware collection (runs locally on the node being inventoried)
# ---------------------------------------------------------------------------

def _run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def collect_hardware():
    """
    Collect hardware facts for the local node and return a dict.
    Dispatches to the platform-specific implementation.
    """
    if platform.system() == 'Darwin':
        return _collect_hardware_darwin()
    return _collect_hardware_linux()


def _collect_hardware_darwin():
    """Collect hardware facts on macOS using system_profiler and sysctl."""
    facts = {
        'collected_at': datetime.now(UTC).isoformat(),
        'cpu': None,
        'ram_gb': None,
        'disks': [],
        'nics': [],
        'gpus': [],
        'serial': None,
        'product': None,
        'bios_version': None,
        'os': None,
        'kernel': None,
    }

    # OS / kernel
    try:
        facts['kernel'] = platform.release()
        facts['os'] = f"macOS {platform.mac_ver()[0]}"
    except Exception:
        pass

    # Hardware overview via system_profiler
    try:
        r = _run(['system_profiler', 'SPHardwareDataType', '-json'], timeout=15)
        data = json.loads(r.stdout)
        hw = data.get('SPHardwareDataType', [{}])[0]
        facts['product'] = hw.get('machine_name')
        facts['serial'] = hw.get('serial_number')
        facts['bios_version'] = hw.get('platform_UUID') and hw.get('SMC_version_system') or hw.get('boot_rom_version')
        mem_str = hw.get('physical_memory', '')  # e.g. "512 GB"
        if mem_str:
            parts = mem_str.split()
            if len(parts) == 2 and parts[1] == 'GB':
                facts['ram_gb'] = int(parts[0])
    except Exception:
        pass

    # CPU via sysctl
    try:
        brand = _run(['sysctl', '-n', 'machdep.cpu.brand_string'], timeout=5).stdout.strip()
        if not brand:
            # Apple Silicon
            brand = _run(['sysctl', '-n', 'machdep.cpu.core_count'], timeout=5).stdout.strip()
            brand = facts.get('product') or 'Apple Silicon'
        phys = _run(['sysctl', '-n', 'hw.physicalcpu'], timeout=5).stdout.strip()
        logical = _run(['sysctl', '-n', 'hw.logicalcpu'], timeout=5).stdout.strip()
        if brand and phys and logical:
            facts['cpu'] = f"{brand}, {phys}c/{logical}t"
        elif brand:
            facts['cpu'] = brand
    except Exception:
        pass

    # Disks via system_profiler
    try:
        r = _run(['system_profiler', 'SPStorageDataType', '-json'], timeout=15)
        data = json.loads(r.stdout)
        seen = set()
        for vol in data.get('SPStorageDataType', []):
            dev = vol.get('bsd_name', '').replace('s0', '').rstrip('0123456789s')
            if not dev or dev in seen:
                continue
            seen.add(dev)
            size_bytes = vol.get('com.apple.diskmanagement.sizeondisk') or 0
            size_gb = int(size_bytes) // (1024 ** 3) if size_bytes else None
            medium = vol.get('physical_interconnect', '').lower()
            disk_type = 'nvme' if 'pcie' in medium or 'nvme' in medium else 'ssd' if 'flash' in medium else 'hdd'
            facts['disks'].append({
                'name': dev,
                'model': vol.get('device_model') or vol.get('_name'),
                'size': f"{size_gb}G" if size_gb else '?',
                'type': disk_type,
            })
    except Exception:
        pass

    # GPUs via system_profiler
    try:
        r = _run(['system_profiler', 'SPDisplaysDataType', '-json'], timeout=15)
        data = json.loads(r.stdout)
        for gpu in data.get('SPDisplaysDataType', []):
            name = gpu.get('sppci_model') or gpu.get('_name')
            vram = gpu.get('sppci_vram') or gpu.get('_spdisplays_vram')
            if name:
                facts['gpus'].append({'vendor': 'Apple' if 'Apple' in name else gpu.get('sppci_vendor', ''), 'model': name, 'vram': vram})
    except Exception:
        pass

    # NICs via system_profiler
    try:
        r = _run(['system_profiler', 'SPNetworkDataType', '-json'], timeout=15)
        data = json.loads(r.stdout)
        for iface in data.get('SPNetworkDataType', []):
            hw_addr = iface.get('spnetwork_hardware_address') or iface.get('Ethernet', {}).get('MAC Address')
            name = iface.get('interface') or iface.get('_name')
            if not name:
                continue
            itype = iface.get('spnetwork_interface_type', '')
            if itype in ('IEEE80211', 'Bridge'):
                continue
            facts['nics'].append({
                'name': name,
                'mac': hw_addr,
                'speed': iface.get('spnetwork_actual_link_speed'),
                'driver': None,
            })
    except Exception:
        pass

    return facts


def _collect_hardware_linux():
    """
    Collect hardware facts for the local Linux node and return a dict.
    Uses dmidecode, lshw, lspci, nvidia-smi where available.
    Degrades gracefully when tools are missing.
    """
    facts = {
        'collected_at': datetime.now(UTC).isoformat(),
        'cpu': None,
        'ram_gb': None,
        'disks': [],
        'nics': [],
        'gpus': [],
        'serial': None,
        'product': None,
        'bios_version': None,
        'os': None,
        'kernel': None,
    }

    # OS / kernel
    try:
        facts['kernel'] = platform.release()
        facts['os'] = ' '.join(_run(['lsb_release', '-sd']).stdout.strip().strip('"').split())
    except Exception:
        pass

    # CPU
    try:
        r = _run(['lscpu'])
        lines = {k.strip(): v.strip() for k, v in
                 (line.split(':', 1) for line in r.stdout.splitlines() if ':' in line)}
        model = lines.get('Model name', '').replace('(R)', '').replace('(TM)', '').strip()
        # CPU(s) = total logical CPUs (threads). Physical cores = sockets * cores-per-socket.
        total_threads = int(lines.get('CPU(s)', 0) or 0)
        sockets = int(lines.get('Socket(s)', 1) or 1)
        cores_per_socket = int(lines.get('Core(s) per socket', 0) or 0)
        phys_cores = sockets * cores_per_socket or total_threads
        if model:
            facts['cpu'] = f"{model}, {phys_cores}c/{total_threads}t" if total_threads else model
    except Exception:
        pass

    # RAM — sum installed DIMMs from dmidecode for exact physical capacity.
    try:
        r = _run(['dmidecode', '-t', 'memory'], timeout=10)
        total_mb = 0
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith('Size:') and 'No Module Installed' not in line:
                parts = line.split()
                if len(parts) >= 3:
                    val, unit = int(parts[1]), parts[2].upper()
                    total_mb += val * 1024 if unit == 'GB' else val
        if total_mb:
            facts['ram_gb'] = total_mb // 1024
    except Exception:
        pass

    # DMI — serial, product, BIOS
    try:
        r = _run(['dmidecode', '-t', 'system'], timeout=10)
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith('Serial Number:'):
                facts['serial'] = line.split(':', 1)[1].strip()
            elif line.startswith('Product Name:'):
                facts['product'] = line.split(':', 1)[1].strip()
    except Exception:
        pass
    try:
        r = _run(['dmidecode', '-t', 'bios'], timeout=10)
        for line in r.stdout.splitlines():
            if line.strip().startswith('Version:'):
                facts['bios_version'] = line.split(':', 1)[1].strip()
                break
    except Exception:
        pass

    # Disks via lsblk
    try:
        r = _run(['lsblk', '-d', '-o', 'NAME,SIZE,ROTA,MODEL', '--json'])
        data = json.loads(r.stdout)
        for dev in data.get('blockdevices', []):
            name = dev.get('name', '')
            # Skip loop, ram, zram devices
            if any(name.startswith(p) for p in ('loop', 'ram', 'zram')):
                continue
            rota = dev.get('rota')
            if isinstance(rota, str):
                rota = rota == '1'
            disk_type = 'hdd' if rota else ('nvme' if name.startswith('nvme') else 'ssd')
            facts['disks'].append({
                'name': name,
                'model': (dev.get('model') or '').strip() or None,
                'size': dev.get('size', '?'),
                'type': disk_type,
            })
    except Exception:
        pass

    # NICs via ip link + ethtool
    try:
        r = _run(['ip', '-j', 'link', 'show'])
        ifaces = json.loads(r.stdout)
        for iface in ifaces:
            name = iface.get('ifname', '')
            if name in ('lo',) or name.startswith(('docker', 'br-', 'veth', 'wg')):
                continue
            link_type = iface.get('link_type', '')
            if link_type not in ('ether',):
                continue
            nic = {'name': name, 'mac': iface.get('address'), 'speed': None, 'driver': None}
            # ethtool for speed — skip "Unknown!" (unplugged/down interfaces)
            try:
                et = _run(['ethtool', name], timeout=3)
                for line in et.stdout.splitlines():
                    if 'Speed:' in line:
                        speed = line.split(':', 1)[1].strip()
                        if speed and speed != 'Unknown!':
                            nic['speed'] = speed
                        break
            except Exception:
                pass
            # driver
            try:
                dr = _run(['ethtool', '-i', name], timeout=3)
                for line in dr.stdout.splitlines():
                    if line.startswith('driver:'):
                        nic['driver'] = line.split(':', 1)[1].strip()
                        break
            except Exception:
                pass
            facts['nics'].append(nic)
    except Exception:
        pass

    # GPUs via lspci
    try:
        r = _run(['lspci', '-mm'])
        for line in r.stdout.splitlines():
            parts = line.split('"')
            # lspci -mm format: slot "class" "vendor" "device" ...
            if len(parts) >= 6:
                cls = parts[1]
                vendor = parts[3]
                device = parts[5]
                if any(c in cls for c in ('VGA', 'Display', '3D', 'Processing Acc')):
                    facts['gpus'].append({'vendor': vendor, 'model': device})
    except Exception:
        pass

    # nvidia-smi for richer GPU info (VRAM, driver)
    try:
        r = _run(['nvidia-smi',
                  '--query-gpu=name,memory.total,driver_version',
                  '--format=csv,noheader,nounits'], timeout=10)
        if r.returncode == 0:
            nvidia_gpus = []
            for line in r.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(',')]
                if len(parts) >= 3:
                    nvidia_gpus.append({
                        'vendor': 'NVIDIA',
                        'model': parts[0],
                        'vram_mb': int(parts[1]) if parts[1].isdigit() else None,
                        'driver': parts[2],
                    })
            if nvidia_gpus:
                # Replace any lspci-detected NVIDIA entries with richer data
                facts['gpus'] = [g for g in facts['gpus'] if 'NVIDIA' not in g.get('vendor', '')]
                facts['gpus'].extend(nvidia_gpus)
    except Exception:
        pass

    return facts
