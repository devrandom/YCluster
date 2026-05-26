#!/usr/bin/env python3
# Emit chassis (IPMI), GPU (nvidia-smi), and Comino RM cooling-controller
# temperatures as Prometheus textfile metrics. Designed to run from a
# short systemd timer on nvidia nodes.
#
# IPMI is the only path to GPU temps for VFIO-passthrough cards: the
# BMC reads the card's thermal diode via the chassis sensor bus,
# independent of which host driver (or none) is bound to the device.
#
# Comino RM (USB-CDC on /dev/ttyACM0) is the only ambient source on
# nv3, which uses a workstation board that doesn't expose an inlet
# sensor via IPMI. See contrib/comino-sensors.py for protocol details.
#
# Output (atomic write to /var/lib/prometheus/node-exporter/temps.prom):
#   node_ipmi_temperature_celsius{sensor="...",role="..."} <value>
#   node_gpu_temperature_celsius{index="...",pci_bus_id="...",name="..."} <value>
#   node_comino_temperature_celsius{sensor="...",role="..."} <value>
#   node_comino_fan_rpm{channel="...",name="..."} <value>
import os
import re
import subprocess
import sys
import tempfile
import time

OUT = "/var/lib/prometheus/node-exporter/temps.prom"

# Comino RM cooling controller (STM32 USB-CDC). If present, the inlet
# air sensor (T3) is our true ambient on workstation-class chassis
# like nv3 where IPMI exposes no INLET_AIR_TEMP equivalent.
COMINO_DEV = "/dev/ttyACM0"
COMINO_FIELDS = [
    "TOT", "CUR", "ALARM", "ERROR", "V",
    "T0", "T1", "T2", "T3", "T4", "T5",
    "RH",
    "CH1", "CH2", "CH3", "CH4", "CH5", "CH6", "CH7", "CH8",
    "FLOW", "LEVEL_ERR", "MB_PS_ON", "FLOW_PPM",
]
COMINO_TEMP_MAP = {
    # field -> (sensor_name, role)
    "T0": ("comino_controller_mcu", "other"),
    "T1": ("comino_coolant_inlet",  "coolant"),
    "T2": ("comino_coolant_outlet", "coolant"),
    "T3": ("comino_air_inlet",      "ambient"),
    "T4": ("comino_air_outlet",     "exhaust"),
    "T5": ("comino_board_i2c",      "other"),
}

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
    # IPMI status codes: ok, nc/cr/nr (threshold crossings — value still
    # real), ns/na (no reading). We keep anything with a parseable
    # number so out-of-threshold readings show up rather than vanish.
    for line in r.stdout.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5:
            continue
        name, _addr, status, _entity, reading = parts[:5]
        if status in ("ns", "na"):
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


def _comino_query(port, cmd, read_timeout=0.5):
    port.reset_input_buffer()
    port.write((cmd + "\r").encode())
    port.flush()
    deadline = time.monotonic() + read_timeout
    buf = bytearray()
    while time.monotonic() < deadline:
        chunk = port.read(256)
        if chunk:
            buf.extend(chunk)
            if buf.endswith(b"\r\n"):
                break
        elif buf:
            break
    return buf.decode(errors="replace").rstrip("\r\n")


def _comino_channel_labels(port):
    raw = _comino_query(port, ":get_channel_value").split(";")
    if len(raw) != 24:
        return {}
    labels = {}
    fan_n = pump_n = 0
    for i in range(8):
        install, _pwm_cal, mode = raw[i * 3:i * 3 + 3]
        ch = f"CH{i + 1}"
        m = mode.strip().upper()
        if install.strip() == "0" or m == "OFF":
            continue
        if m == "FAN":
            fan_n += 1
            labels[ch] = f"fan {fan_n}"
        elif m == "PUMP":
            pump_n += 1
            labels[ch] = f"pump {pump_n}"
        else:
            labels[ch] = f"{ch} ({m.lower() or '?'})"
    return labels


def collect_comino():
    """Return (temps, fans). temps = [(sensor, role, c)]; fans = [(ch, name, rpm)]."""
    temps, fans = [], []
    if not os.path.exists(COMINO_DEV):
        return temps, fans
    try:
        import serial
    except ImportError:
        print("# pyserial not installed; skipping Comino", file=sys.stderr)
        return temps, fans

    # rm-monitor (if installed) grabs the port via TIOCEXCL during 5s
    # polls, so retry briefly on EBUSY.
    last_err = None
    for _ in range(5):
        try:
            with serial.Serial(COMINO_DEV, 9600, timeout=0.1, exclusive=True) as port:
                ch_labels = _comino_channel_labels(port)
                line = _comino_query(port, ":get_data")
            break
        except (serial.SerialException, OSError) as e:
            last_err = e
            time.sleep(0.3)
    else:
        print(f"# comino read failed: {last_err}", file=sys.stderr)
        return temps, fans

    parts = line.split(";")
    if len(parts) != len(COMINO_FIELDS):
        print(f"# comino unexpected field count {len(parts)}: {line!r}", file=sys.stderr)
        return temps, fans
    data = dict(zip(COMINO_FIELDS, parts))

    for field, (sensor, role) in COMINO_TEMP_MAP.items():
        try:
            v = float(data[field].replace(",", "."))
        except (KeyError, ValueError):
            continue
        # Controller sometimes reports 0/negative for disconnected probes.
        if v <= 0 or v > 150:
            continue
        temps.append((sensor, role, v))

    for ch, name in ch_labels.items():
        try:
            v = float(data[ch].replace(",", "."))
        except (KeyError, ValueError):
            continue
        if v < 0:
            continue
        fans.append((ch, name, v))

    return temps, fans


def esc(v):
    return v.replace("\\", "\\\\").replace('"', '\\"')


def write_prom(ipmi, gpu, comino_temps, comino_fans):
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
    lines.append("# HELP node_comino_temperature_celsius Temperature from a Comino RM cooling controller.")
    lines.append("# TYPE node_comino_temperature_celsius gauge")
    for sensor, role, val in comino_temps:
        lines.append(
            f'node_comino_temperature_celsius{{sensor="{esc(sensor)}",role="{role}"}} {val}'
        )
    lines.append("# HELP node_comino_fan_rpm Fan/pump tachometer from a Comino RM cooling controller.")
    lines.append("# TYPE node_comino_fan_rpm gauge")
    for ch, name, val in comino_fans:
        lines.append(
            f'node_comino_fan_rpm{{channel="{esc(ch)}",name="{esc(name)}"}} {val}'
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
    comino_temps, comino_fans = collect_comino()
    if not ipmi and not gpu and not comino_temps:
        # Don't clobber a previously-good file with an empty one;
        # that would make alerts flap on transient ipmitool errors.
        print("# no readings; leaving previous file in place", file=sys.stderr)
        return 1
    write_prom(ipmi, gpu, comino_temps, comino_fans)
    return 0


if __name__ == "__main__":
    sys.exit(main())
