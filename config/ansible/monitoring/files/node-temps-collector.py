#!/usr/bin/env python3
# Emit chassis (IPMI), GPU (nvidia-smi), and Comino RM cooling-controller
# temperatures as Prometheus textfile metrics. Designed to run from a
# short systemd timer on nvidia nodes.
#
# IPMI is the only path to GPU temps for VFIO-passthrough cards: the
# BMC reads the card's thermal diode via the chassis sensor bus,
# independent of which host driver (or none) is bound to the device.
#
# Comino RM (an STM32 USB-CDC device) is the only ambient source on
# workstation boards that don't expose an inlet sensor via IPMI. It is
# auto-discovered among the /dev/ttyACM* devices rather than assumed to
# be ttyACM0: a chassis may expose a second STM32 ACM device (e.g. a
# CRPS power-supply controller) that enumerates first and takes ttyACM0.
# See contrib/comino-sensors.py for protocol details.
#
# Output (atomic write to /var/lib/prometheus/node-exporter/temps.prom):
#   node_ipmi_temperature_celsius{sensor="...",role="..."} <value>
#   node_gpu_temperature_celsius{index="...",pci_bus_id="...",name="..."} <value>
#   node_gpu_thermal_slowdown{index="...",pci_bus_id="...",name="...",reason="hw"|"sw"} 0|1
#   node_gpu_ecc_errors_{corrected,uncorrected}_total{...} <value>
#   node_comino_temperature_celsius{sensor="...",role="..."} <value>
#   node_comino_fan_rpm{channel="...",name="..."} <value>
#   node_gpu_flr_events_total <value>
#   node_gpu_flr_probe_ok <value>
import glob
import os
import re
import subprocess
import sys
import tempfile
import time

OUT = "/var/lib/prometheus/node-exporter/temps.prom"

# Host-journal evidence of a PCIe Function Level Reset (FLR). A reset is
# node-level evidence: journals generally do not identify the GPU in the
# same field across drivers/kernels, so the metric is deliberately not
# GPU-labeled.
FLR_PATTERN = re.compile(r"function level reset|\bFLR\b", re.I)

# Comino RM cooling controller (STM32 USB-CDC). If present, the inlet
# air sensor (T3) is our true ambient on workstation-class chassis where
# IPMI exposes no INLET_AIR_TEMP equivalent. The controller is found by
# probing each candidate device for a well-formed :get_data reply rather
# than hardcoding a device node (see comino_candidate_devices).
COMINO_GLOB = "/dev/ttyACM*"
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


def _collect_throttle():
    """Return {pci_bus_id: (hw_thermal_active, sw_thermal_active)}.

    Runs as a second nvidia-smi call so older drivers without the
    clocks_throttle_reasons query fields degrade to no throttle metrics
    instead of losing temperature collection entirely.
    """
    query = (
        "index,pci.bus_id,"
        "clocks_throttle_reasons.hw_thermal_slowdown,"
        "clocks_throttle_reasons.sw_thermal_slowdown"
    )
    try:
        r = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if r.returncode != 0:
        return {}
    out = {}
    for line in r.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 4:
            continue
        out[parts[1]] = (parts[2] == "Active", parts[3] == "Active")
    return out


def _optional_float(value):
    """Parse nvidia-smi numbers, returning None for N/A/unsupported fields."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None



def collect_nvidia():
    """Return list of dicts for each host-visible GPU."""
    out = []
    query = (
        "index,pci.bus_id,name,"
        "temperature.gpu,utilization.gpu,utilization.memory,"
        "memory.used,memory.total,power.draw,"
        "ecc.errors.corrected.volatile.total,"
        "ecc.errors.uncorrected.volatile.total"
    )
    try:
        r = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}",
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
        if len(parts) != 11:
            continue
        temp_c = _optional_float(parts[3])
        util_gpu = _optional_float(parts[4])
        util_mem = _optional_float(parts[5])
        mem_used_mib = _optional_float(parts[6])
        mem_total_mib = _optional_float(parts[7])
        power_w = _optional_float(parts[8])
        if None in (temp_c, util_gpu, util_mem, mem_used_mib,
                    mem_total_mib, power_w):
            continue
        out.append({
            "index":         parts[0],
            "pci_bus_id":    parts[1],
            "name":          parts[2],
            "temp_c":        temp_c,
            "util_gpu":      util_gpu,
            "util_mem":      util_mem,
            "mem_used_mib":  mem_used_mib,
            "mem_total_mib": mem_total_mib,
            "power_w":       power_w,
            "ecc_corrected": _optional_float(parts[9]),
            "ecc_uncorrected": _optional_float(parts[10]),
        })

    throttle = _collect_throttle()
    for gpu in out:
        gpu["hw_slow"], gpu["sw_slow"] = throttle.get(
            gpu["pci_bus_id"], (False, False))
    return out


def collect_flr_events():
    """Return (events_since_boot, probe_ok) from the current boot's journal."""
    try:
        r = subprocess.run(
            ["journalctl", "--boot", "--no-pager", "--output=cat",
             "--grep", "function level reset|\\bFLR\\b"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"# journalctl failed: {e}", file=sys.stderr)
        return 0, 0
    if r.returncode != 0:
        # journalctl has no "exactly zero matches" success status; rc=1
        # with no stderr means no matching journal records this boot.
        if r.returncode != 1 or r.stderr.strip():
            print(f"# journalctl rc={r.returncode}: {r.stderr.strip()}",
                  file=sys.stderr)
            return 0, 0
        return 0, 1
    return sum(1 for line in r.stdout.splitlines()
               if FLR_PATTERN.search(line)), 1


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


def _comino_channel_label(install, mode, ch, fan_n, pump_n):
    """Return (label, fan_n, pump_n) for one channel, or (None, ...) to skip."""
    if install.strip() == "0" or mode == "OFF":
        return None, fan_n, pump_n
    if mode == "FAN":
        fan_n += 1
        return f"fan {fan_n}", fan_n, pump_n
    if mode == "PUMP":
        pump_n += 1
        return f"pump {pump_n}", fan_n, pump_n
    return f"{ch} ({mode.lower() or '?'})", fan_n, pump_n


def _comino_channel_labels(port):
    raw = _comino_query(port, ":get_channel_value").split(";")
    if len(raw) != 24:
        return {}
    labels = {}
    fan_n = pump_n = 0
    for i in range(8):
        install, _pwm_cal, mode = raw[i * 3:i * 3 + 3]
        ch = f"CH{i + 1}"
        label, fan_n, pump_n = _comino_channel_label(
            install, mode.strip().upper(), ch, fan_n, pump_n)
        if label is not None:
            labels[ch] = label
    return labels


def comino_candidate_devices():
    """USB-CDC ACM devices that might be the Comino RM controller."""
    return sorted(glob.glob(COMINO_GLOB))


def parse_comino_data(line):
    """Parse a :get_data reply into a field->value dict, or None if the
    line doesn't look like a Comino RM response. A second STM32 ACM
    device (e.g. a CRPS power-supply controller) shares the USB-CDC VID
    but answers :get_data with a 'command not found' error, which fails
    the field-count check here."""
    parts = line.split(";")
    if len(parts) != len(COMINO_FIELDS):
        return None
    return dict(zip(COMINO_FIELDS, parts))


def select_comino_device(devices, reader):
    """Return (dev, data, labels) for the first device whose reader()
    yields a well-formed Comino reply, or None if none match. reader(dev)
    returns (line, channel_labels) or None on an I/O failure."""
    for dev in devices:
        res = reader(dev)
        if res is None:
            continue
        line, labels = res
        data = parse_comino_data(line)
        if data is not None:
            return dev, data, labels
    return None


def _comino_reader(dev):
    """Return (data, channel_labels) from a Comino serial device, retrying
    on transient EBUSY. Returns None when all retries fail."""

    # rm-monitor (if installed) grabs the port via TIOCEXCL during 5s
    # polls, so retry briefly on EBUSY.
    import serial

    last_err = None
    for _ in range(5):
        try:
            with serial.Serial(dev, 9600, timeout=0.1, exclusive=True) as port:
                labels = _comino_channel_labels(port)
                line = _comino_query(port, ":get_data")
            return line, labels
        except (serial.SerialException, OSError) as e:
            last_err = e
            time.sleep(0.3)
    print(f"# comino read failed on {dev}: {last_err}", file=sys.stderr)
    return None


def _comino_parse_temps(data, temp_map):
    """Extract valid temperature readings from the controller payload."""
    temps = []
    for field, (sensor, role) in temp_map.items():
        try:
            v = float(data[field].replace(",", "."))
        except (KeyError, ValueError):
            continue
        # Controller sometimes reports 0/negative for disconnected probes.
        if 0 < v <= 150:
            temps.append((sensor, role, v))
    return temps


def _comino_parse_fans(data, ch_labels):
    """Extract valid fan RPM readings from the controller payload."""
    fans = []
    for ch, name in ch_labels.items():
        try:
            v = float(data[ch].replace(",", "."))
        except (KeyError, ValueError):
            continue
        if v >= 0:
            fans.append((ch, name, v))
    return fans


def collect_comino():
    """Return (temps, fans). temps = [(sensor, role, c)]; fans = [(ch, name, rpm)]."""
    temps, fans = [], []
    try:
        import serial
    except ImportError:
        print("# pyserial not installed; skipping Comino", file=sys.stderr)
        return temps, fans

    selected = select_comino_device(comino_candidate_devices(), _comino_reader)
    if selected is None:
        return temps, fans
    _dev, data, ch_labels = selected

    return (_comino_parse_temps(data, COMINO_TEMP_MAP),
            _comino_parse_fans(data, ch_labels))


def esc(v):
    return v.replace("\\", "\\\\").replace('"', '\\"')


def _gpu_labels(g):
    return (
        f'index="{esc(g["index"])}",'
        f'pci_bus_id="{esc(g["pci_bus_id"])}",'
        f'name="{esc(g["name"])}"'
    )


def _emit_metric(lines, help_text, name, samples, metric_type="gauge"):
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {metric_type}")
    lines.extend(samples)


def _emit_gpu_metrics(lines, gpu):
    lbls = _gpu_labels
    _emit_metric(lines, "GPU temperature via nvidia-smi (host-visible GPUs only).",
                 "node_gpu_temperature_celsius",
                 [f'node_gpu_temperature_celsius{{{lbls(g)}}} {g["temp_c"]}' for g in gpu])
    _emit_metric(lines, "GPU compute utilization percent (host-visible GPUs).",
                 "node_gpu_utilization_percent",
                 [f'node_gpu_utilization_percent{{{lbls(g)}}} {g["util_gpu"]}' for g in gpu])
    _emit_metric(lines, "GPU memory-bandwidth utilization percent.",
                 "node_gpu_memory_utilization_percent",
                 [f'node_gpu_memory_utilization_percent{{{lbls(g)}}} {g["util_mem"]}' for g in gpu])
    _emit_metric(lines, "GPU memory used in bytes.",
                 "node_gpu_memory_used_bytes",
                 [f'node_gpu_memory_used_bytes{{{lbls(g)}}} {g["mem_used_mib"] * 1024 * 1024}' for g in gpu])
    _emit_metric(lines, "GPU memory total in bytes.",
                 "node_gpu_memory_total_bytes",
                 [f'node_gpu_memory_total_bytes{{{lbls(g)}}} {g["mem_total_mib"] * 1024 * 1024}' for g in gpu])
    _emit_metric(lines, "GPU power draw in watts.",
                 "node_gpu_power_watts",
                 [f'node_gpu_power_watts{{{lbls(g)}}} {g["power_w"]}' for g in gpu])
    _emit_metric(
        lines,
        "GPU thermal slowdown active right now: reason hw (hardware) or "
        "sw (software/driver). 1 = throttling.",
        "node_gpu_thermal_slowdown",
        [f'node_gpu_thermal_slowdown{{{lbls(g)},reason="{r}"}} '
         f'{1 if g[f"{r}_slow"] else 0}'
         for g in gpu for r in ("hw", "sw")])
    _emit_metric(
        lines,
        "Corrected volatile ECC errors reported by nvidia-smi.",
        "node_gpu_ecc_errors_corrected_total",
        [f'node_gpu_ecc_errors_corrected_total{{{lbls(g)}}} {g["ecc_corrected"]}'
         for g in gpu if g.get("ecc_corrected") is not None],
        metric_type="counter")
    _emit_metric(
        lines,
        "Uncorrected volatile ECC errors reported by nvidia-smi.",
        "node_gpu_ecc_errors_uncorrected_total",
        [f'node_gpu_ecc_errors_uncorrected_total{{{lbls(g)}}} {g["ecc_uncorrected"]}'
         for g in gpu if g.get("ecc_uncorrected") is not None],
        metric_type="counter")


def _emit_flr_metrics(lines, flr_events, flr_probe_ok):
    _emit_metric(
        lines,
        "PCIe Function Level Resets seen in the host journal this boot.",
        "node_gpu_flr_events_total",
        [f"node_gpu_flr_events_total {flr_events}"],
        metric_type="counter")
    _emit_metric(
        lines,
        "Whether the host journal FLR probe completed successfully.",
        "node_gpu_flr_probe_ok",
        [f"node_gpu_flr_probe_ok {flr_probe_ok}"])


def _prom_body(ipmi, gpu, comino_temps, comino_fans,
               flr_events, flr_probe_ok):
    """Render all collected metrics in Prometheus text format."""
    lines = []
    _emit_metric(lines, "Chassis temperature sensor reading via IPMI.",
                 "node_ipmi_temperature_celsius",
                 [f'node_ipmi_temperature_celsius{{sensor="{esc(sensor)}",role="{role}"}} {val}'
                  for sensor, role, val in ipmi])
    _emit_gpu_metrics(lines, gpu)
    _emit_flr_metrics(lines, flr_events, flr_probe_ok)
    _emit_metric(lines, "Temperature from a Comino RM cooling controller.",
                 "node_comino_temperature_celsius",
                 [f'node_comino_temperature_celsius{{sensor="{esc(sensor)}",role="{role}"}} {val}'
                  for sensor, role, val in comino_temps])
    _emit_metric(lines, "Fan/pump tachometer from a Comino RM cooling controller.",
                 "node_comino_fan_rpm",
                 [f'node_comino_fan_rpm{{channel="{esc(ch)}",name="{esc(name)}"}} {val}'
                  for ch, name, val in comino_fans])
    return "\n".join(lines) + "\n"


def write_prom(ipmi, gpu, comino_temps, comino_fans,
               flr_events, flr_probe_ok):
    body = _prom_body(ipmi, gpu, comino_temps, comino_fans,
                      flr_events, flr_probe_ok)

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
    flr_events, flr_probe_ok = collect_flr_events()
    # Always rewrite the file so node_textfile_mtime_seconds advances
    # even when individual sub-collectors come up empty. The mtime
    # staleness alert fires only when the script itself stops running.
    write_prom(ipmi, gpu, comino_temps, comino_fans,
               flr_events, flr_probe_ok)
    return 0


if __name__ == "__main__":
    sys.exit(main())
