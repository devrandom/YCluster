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

# Disks
seen = set()
for vol in (sp.get('SPStorageDataType') or []):
    dev = vol.get('bsd_name', '').rstrip('0123456789').rstrip('s')
    if not dev or dev in seen:
        continue
    seen.add(dev)
    size_bytes = vol.get('com.apple.diskmanagement.sizeondisk') or 0
    size_g = int(size_bytes) // (1024 ** 3) if size_bytes else 0
    medium = vol.get('physical_interconnect', '').lower()
    disk_type = 'nvme' if ('pcie' in medium or 'nvme' in medium) else 'ssd' if 'flash' in medium else 'hdd'
    facts['disks'].append({
        'name': dev,
        'model': vol.get('device_model') or vol.get('_name'),
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

# NICs
for iface in (sp.get('SPNetworkDataType') or []):
    name = iface.get('interface') or iface.get('_name')
    if not name or iface.get('spnetwork_interface_type') in ('IEEE80211', 'Bridge'):
        continue
    facts['nics'].append({
        'name': name,
        'mac': iface.get('spnetwork_hardware_address'),
        'speed': iface.get('spnetwork_actual_link_speed'),
        'driver': None,
    })

print(json.dumps(facts))
