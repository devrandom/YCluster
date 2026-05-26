#!/usr/bin/env python3
"""
Read live sensor data from a Comino RM cooling controller.

Talks to the STM32 USB-CDC controller (default /dev/ttyACM0) at 9600 8N1 using
the line protocol reverse-engineered from rm-monitor's plugin:

    :get_data\r        -> CSV of live values
    :screen\r          -> human-readable display text
    :get_log_num\r     -> stored log count
    :get_log N\r       -> CSV log entry N

Field order of :get_data (matches rm-monitor.db columns 1:1):
    TOT CUR ALARM ERROR V T0 T1 T2 T3 T4 T5 RH
    CH1..CH8 FLOW LEVEL_ERR MB_PS_ON FLOW_PPM

Per Comino docs:
    T0    STM controller temp
    T1/T2 coolant inlet/outlet
    T3/T4 air inlet/outlet
    T5    board temp+humidity (I2C)
    CH1..CH6 = F1..F6 fan tachs (RPM)
    CH7..CH8 = P1..P2 pump tachs (RPM)
    FLOW  l/min ; FLOW_PPM raw flow-meter pulses
    V     12V rail voltage
    RH    relative humidity (%)

The rm-monitor plugin grabs the port with TIOCEXCL during each ~5s poll, so
expect occasional EBUSY collisions. Stop the service for clean access:
    systemctl stop rm-monitor-server.service
"""
import argparse
import json
import sys
import time

import serial

FIELDS = [
    "TOT", "CUR", "ALARM", "ERROR", "V",
    "T0", "T1", "T2", "T3", "T4", "T5",
    "RH",
    "CH1", "CH2", "CH3", "CH4", "CH5", "CH6", "CH7", "CH8",
    "FLOW", "LEVEL_ERR", "MB_PS_ON", "FLOW_PPM",
]

# Fixed by controller firmware (per Comino wiki).
TEMP_LABELS = {
    "T0": ("controller MCU", "C"),
    "T1": ("coolant inlet", "C"),
    "T2": ("coolant outlet", "C"),
    "T3": ("air inlet", "C"),
    "T4": ("air outlet", "C"),
    "T5": ("board (I2C)", "C"),
}

OTHER_LABELS = {
    "V":         ("12V rail",            "V"),
    "RH":        ("board humidity",      "%"),
    "FLOW":      ("coolant flow",        "l/min"),
    "FLOW_PPM":  ("flow meter raw",      "ppm"),
    "TOT":       ("total run time",      "ms"),
    "CUR":       ("current up time",     "ms"),
    "ALARM":     ("alarm",               ""),
    "ERROR":     ("error",               ""),
    "LEVEL_ERR": ("coolant-level error", ""),
    "MB_PS_ON":  ("mainboard PSU on",    ""),
}


def parse_value(s: str):
    s = s.strip()
    if s == "" or s.lower() == "null":
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return s


def query(port: serial.Serial, cmd: str, read_timeout: float = 0.5) -> str:
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
        else:
            if buf:
                break
    return buf.decode(errors="replace").rstrip("\r\n")


def get_channel_labels(port: serial.Serial) -> dict:
    """Query :get_channel_value and return {CH1: 'fan_1', CH7: 'pump_1', ...}."""
    raw = query(port, ":get_channel_value").split(";")
    if len(raw) != 24:
        return {}
    labels = {}
    fan_n = pump_n = 0
    for i in range(8):
        install, _pwm_cal, mode = raw[i * 3:i * 3 + 3]
        ch = f"CH{i + 1}"
        m = mode.strip().upper()
        if install.strip() == "0" or m == "OFF":
            labels[ch] = (f"{ch} (unused)", "rpm")
        elif m == "FAN":
            fan_n += 1
            labels[ch] = (f"fan {fan_n}", "rpm")
        elif m == "PUMP":
            pump_n += 1
            labels[ch] = (f"pump {pump_n}", "rpm")
        else:
            labels[ch] = (f"{ch} ({m.lower() or '?'})", "rpm")
    return labels


def get_data(device: str) -> tuple[dict, dict]:
    with serial.Serial(device, 9600, timeout=0.1, exclusive=True) as port:
        ch_labels = get_channel_labels(port)
        line = query(port, ":get_data")
    parts = line.split(";")
    if len(parts) != len(FIELDS):
        raise ValueError(
            f"unexpected field count {len(parts)} (expected {len(FIELDS)}): {line!r}"
        )
    data = {name: parse_value(v) for name, v in zip(FIELDS, parts)}
    labels = {**OTHER_LABELS, **TEMP_LABELS, **ch_labels}
    return data, labels


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--device", default="/dev/ttyACM0")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of table")
    ap.add_argument("--retries", type=int, default=5,
                    help="retries on EBUSY (rm-monitor holding port)")
    args = ap.parse_args()

    last_err = None
    for attempt in range(args.retries):
        try:
            data, labels = get_data(args.device)
            break
        except (serial.SerialException, OSError) as e:
            last_err = e
            time.sleep(0.3)
    else:
        print(f"failed after {args.retries} attempts: {last_err}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        out = {k: {"value": data[k],
                   "label": labels.get(k, (k, ""))[0],
                   "unit":  labels.get(k, (k, ""))[1]} for k in FIELDS}
        json.dump(out, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    for k in FIELDS:
        v = data[k]
        label, unit = labels.get(k, (k, ""))
        name = f"{label} ({k})"
        if v is None:
            print(f"  {name:<28} null")
        elif isinstance(v, float):
            print(f"  {name:<28} {v:>10.2f} {unit}")
        else:
            print(f"  {name:<28} {v} {unit}")


if __name__ == "__main__":
    main()
