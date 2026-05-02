#!/usr/bin/env python3
"""
Collect hardware facts on macOS using system_profiler and sysctl.
Prints a JSON object to stdout.
Usage: python3 collect-hw-macos.py
"""
import json
import subprocess
import datetime
import sys


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


sp_raw = run(['system_profiler',
              'SPHardwareDataType', 'SPStorageDataType',
              'SPDisplaysDataType', 'SPNetworkDataType', '-json'])
sp = json.loads(sp_raw) if sp_raw else {}

sysctl_out = run(['sysctl',
                  'hw.physicalcpu', 'hw.logicalcpu',
                  'hw.memsize', 'machdep.cpu.brand_string'])
sysctl = dict(
    line.split(': ', 1) for line in sysctl_out.splitlines() if ': ' in line
)

facts = {
    'collected_at': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S+00:00'),
    'os': 'macOS ' + run(['sw_vers', '-productVersion']),
    'kernel': run(['uname', '-r']),
    'cpu': None, 'ram_gb': None,
    'disks': [], 'nics': [], 'gpus': [],
    'serial': None, 'product': None, 'bios_version': None,
}

# Hardware overview
hw = (sp.get('SPHardwareDataType') or [{}])[0]
facts['product'] = hw.get('machine_name')
facts['serial'] = hw.get('serial_number')
facts['bios_version'] = hw.get('SMC_version_system') or hw.get('boot_rom_version')
mem_str = hw.get('physical_memory', '')
if mem_str:
    parts = mem_str.split()
    if len(parts) == 2 and parts[1] == 'GB':
        facts['ram_gb'] = int(parts[0])

# CPU
brand = sysctl.get('machdep.cpu.brand_string', '').strip() or facts.get('product') or 'Apple Silicon'
phys = sysctl.get('hw.physicalcpu', '').strip()
logical = sysctl.get('hw.logicalcpu', '').strip()
facts['cpu'] = (brand + ', ' + phys + 'c/' + logical + 't') if phys and logical else brand

# Disks — deduplicate by physical device name to avoid listing every
# APFS volume separately. Use physical_drive metadata for type/model.
seen_models = set()
for vol in (sp.get('SPStorageDataType') or []):
    pd = vol.get('physical_drive') or {}
    model = pd.get('device_name', '').strip()
    if not model or model == 'Disk Image' or model in seen_models:
        continue
    seen_models.add(model)
    size_bytes = vol.get('size_in_bytes') or 0
    size_g = int(size_bytes) // (1024 ** 3) if size_bytes else 0
    protocol = pd.get('protocol', '').lower()
    medium_type = pd.get('medium_type', '').lower()
    disk_type = 'nvme' if 'nvme' in protocol or 'pcie' in protocol or 'fabric' in protocol else \
                'ssd' if 'ssd' in medium_type or 'flash' in medium_type else 'hdd'
    facts['disks'].append({
        'name': vol.get('bsd_name', ''),
        'model': model,
        'size': str(size_g) + 'G' if size_g else '?',
        'type': disk_type,
    })

# GPUs
for gpu in (sp.get('SPDisplaysDataType') or []):
    name = gpu.get('sppci_model') or gpu.get('_name')
    if name:
        facts['gpus'].append({
            'vendor': 'Apple' if 'Apple' in name else gpu.get('sppci_vendor', ''),
            'model': name,
            'vram': gpu.get('sppci_vram') or gpu.get('_spdisplays_vram'),
        })

# NICs — only physical Ethernet interfaces
skip_types = {'IEEE80211', 'Bridge', 'FireWire', 'Thunderbolt'}
for iface in (sp.get('SPNetworkDataType') or []):
    name = iface.get('interface') or iface.get('_name')
    if not name:
        continue
    if iface.get('spnetwork_interface_type') in skip_types:
        continue
    # Skip virtual/bridge/loopback by name prefix
    if name.startswith(('bridge', 'lo', 'utun', 'ipsec', 'gif', 'stf', 'anpi')):
        continue
    eth = iface.get('Ethernet') or {}
    facts['nics'].append({
        'name': name,
        'mac': eth.get('MAC Address') or iface.get('spnetwork_hardware_address'),
        'speed': eth.get('MediaSpeed') or iface.get('spnetwork_actual_link_speed'),
        'driver': None,
    })

print(json.dumps(facts))
