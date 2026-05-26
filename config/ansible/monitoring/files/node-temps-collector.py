#!/usr/bin/env python3
# Emit chassis (IPMI) and GPU (nvidia-smi) temperatures as Prometheus
# textfile metrics. Designed to run from a short systemd timer on
# nvidia nodes.
#
# IPMI is the only path to GPU temps for VFIO-passthrough cards: the
# BMC reads the card's thermal diode via the chassis sensor bus,
# independent of which host driver (or none) is bound to the device.
#
# Output (atomic write to /var/lib/prometheus/node-exporter/temps.prom):
#   node_ipmi_temperature_celsius{sensor="...",role="..."} <value>
#   node_gpu_temperature_celsius{index="...",pci_bus_id="...",name="..."} <value>
import os
import re
import subprocess
import sys
import tempfile

OUT = "/var/lib/prometheus/node-exporter/temps.prom"

# Classify IPMI sensor names into roles so alert rules can match by
# label rather than by name regex. Order matters: first match wins.
ROLE_PATTERNS = [
    ("ambient", re.compile(r"(INLET|INTAKE|AMBIENT|AIR_TEMP)", re.I)),
    ("gpu",     re.compile(r"(GPU_TEMP|^PCIE\d+\s*Temp)", re.I)),
    ("cpu",     re.compile(r"(CPU.*TEMP|CPU.*Package)", re.I)),
    ("memory",  re.compile(r"DIMM", re.I)),
    ("psu",     re.compile(r"PSU", re.I)),
    ("disk",    re.compile(r"(HDD|NVME|SSD|M2_)", re.I)),
]


def role_for(sensor):
    for role, pat in ROLE_PATTERNS:
        if pat.search(sensor):
            return role
    return "other"


def collect_ipmi():
    """Return list of (sensor_name, role, celsius) from `ipmitool sdr type Temperature`."""
    out = []
    try:
        r = subprocess.run(
            ["ipmitool", "sdr", "type", "Temperature"],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"# ipmitool failed: {e}", file=sys.stderr)
        return out
    if r.returncode != 0:
        print(f"# ipmitool rc={r.returncode}: {r.stderr.strip()}", file=sys.stderr)
        return out
    # Lines look like:
    #   "INLET_AIR_TEMP   | 0Bh | ok  | 55.1 | 20 degrees C"
    #   "SLOT1_GPU_TEMP   | 18h | ns  | 11.1 | No Reading"
    for line in r.stdout.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5:
            continue
        name, _addr, status, _entity, reading = parts[:5]
        if status != "ok":
            continue
        m = re.match(r"(-?\d+(?:\.\d+)?)\s*degrees\s*C", reading)
        if not m:
            continue
        out.append((name, role_for(name), float(m.group(1))))
    return out


def collect_nvidia():
    """Return list of (index, pci_bus_id, name, celsius) from nvidia-smi."""
    out = []
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,pci.bus_id,name,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"# nvidia-smi failed: {e}", file=sys.stderr)
        return out
    if r.returncode != 0:
        # No host-visible GPUs (everything bound to vfio-pci) is normal,
        # not an error worth surfacing.
        return out
    for line in r.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 4:
            continue
        idx, pci, name, temp = parts
        try:
            t = float(temp)
        except ValueError:
            continue
        out.append((idx, pci, name, t))
    return out


def esc(v):
    return v.replace("\\", "\\\\").replace('"', '\\"')


def write_prom(ipmi, gpu):
    lines = []
    lines.append("# HELP node_ipmi_temperature_celsius Chassis temperature sensor reading via IPMI.")
    lines.append("# TYPE node_ipmi_temperature_celsius gauge")
    for sensor, role, val in ipmi:
        lines.append(
            f'node_ipmi_temperature_celsius{{sensor="{esc(sensor)}",role="{role}"}} {val}'
        )
    lines.append("# HELP node_gpu_temperature_celsius GPU temperature via nvidia-smi (host-visible GPUs only).")
    lines.append("# TYPE node_gpu_temperature_celsius gauge")
    for idx, pci, name, val in gpu:
        lines.append(
            f'node_gpu_temperature_celsius{{index="{esc(idx)}",pci_bus_id="{esc(pci)}",name="{esc(name)}"}} {val}'
        )
    body = "\n".join(lines) + "\n"

    d = os.path.dirname(OUT)
    fd, tmp = tempfile.mkstemp(prefix=".temps.", suffix=".prom", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(body)
        os.chmod(tmp, 0o644)
        os.rename(tmp, OUT)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main():
    ipmi = collect_ipmi()
    gpu = collect_nvidia()
    if not ipmi and not gpu:
        # Don't clobber a previously-good file with an empty one;
        # that would make alerts flap on transient ipmitool errors.
        print("# no readings; leaving previous file in place", file=sys.stderr)
        return 1
    write_prom(ipmi, gpu)
    return 0


if __name__ == "__main__":
    sys.exit(main())
