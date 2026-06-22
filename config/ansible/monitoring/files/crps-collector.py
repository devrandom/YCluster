#!/usr/bin/env python3
# Emit Comino CRPS power-supply telemetry as Prometheus textfile metrics.
# Designed to run from a short systemd timer on nvidia nodes, like
# node-temps-collector. See docs/design/comino-crps-power-monitoring.md.
#
# On Comino workstation-class GPU chassis the BMC exposes no usable PSU
# telemetry (sensors read na, DCMI power is 0 W). The only real source of
# per-PSU input voltage, output power, and fault state is the Comino CRPS
# controller: a second STM32 USB-CDC device (separate from the RM cooling
# controller) that aggregates the chassis power supplies over PMBus and
# speaks a `crps>` line shell.
#
# Two distinct STM32 ACM devices can appear on a Comino chassis and their
# /dev/ttyACM* enumeration order is not fixed, so the controller is found
# by probing each candidate for a parseable `show crps` reply rather than
# hardcoding a device node. The cooling controller answers `show crps`
# with "ERROR: unknown command 'show'" and is skipped. Only read-only
# `show` commands are ever issued — never reboot/restore/backup/setup,
# which write config or reset the controller.
#
# This collector publishes two data paths (see the design doc):
#
#  1. Live gauges — current per-PSU readings, controller state, redundancy
#     config from `show crps` / `show alarms` / `show params`. These catch
#     steady-state and slow trends but miss sub-second transients (an AC
#     sag or single-PSU dropout that recovers between scrapes).
#
#  2. Event harvest — the controller logs every significant state change
#     (Normal/Alarm/Fault) to an on-board ring buffer (`show log`), which
#     IS where those missed transients survive. We tail it and republish
#     each distinct entry as a Prometheus counter, so a transient the
#     gauges rode over still shows up as `increase(node_crps_log_event_
#     total{state="fault"}[...]) > 0`, stamped at the collector's own
#     observation time.
#
# `show log` is stateful and awkward: each call advances a device-side
# paging cursor by one (it persists across reconnects and wraps at the
# ring size), there is no random access, and the ring spans multiple
# power-on epochs so its `ts` (seconds-since-power-on) is NOT globally
# monotonic. We therefore identify entries by a content hash — robust to
# the cursor position and to cross-epoch disorder — sweep a bounded page
# budget per scrape (lapping the whole ring every few minutes), and keep
# a small persistent state file of seen-hashes + counters so the counters
# survive across the oneshot timer's invocations. The first run seeds the
# seen-set from a full ring sweep WITHOUT counting, so the pre-existing
# backlog isn't replayed as if it just happened.
#
# Two distinct STM32 ACM devices can appear on a Comino chassis and their
# /dev/ttyACM* enumeration order is not fixed, so the controller is found
# by probing each candidate for a parseable `show crps` reply rather than
# hardcoding a device node. The cooling controller answers `show crps`
# with "ERROR: unknown command 'show'" and is skipped. Only read-only
# `show` commands are ever issued — never reboot/restore/backup/setup,
# which write config or reset the controller.
#
# Output (atomic write to /var/lib/prometheus/node-exporter/crps.prom):
#   node_crps_psu_vin_volts{module}
#   node_crps_psu_vout_volts{module}
#   node_crps_psu_iout_amps{module}
#   node_crps_psu_pin_watts{module}
#   node_crps_psu_pout_watts{module}
#   node_crps_psu_temp_celsius{module,probe}
#   node_crps_psu_fan_rpm{module}
#   node_crps_psu_present{module}
#   node_crps_psu_ok{module}
#   node_crps_state{state="normal|alarm|fault"}
#   node_crps_alarm{flag}
#   node_crps_work_qty
#   node_crps_reserve_qty
#   node_crps_redundancy_enabled
#   node_crps_log_event_total{state}        harvested log entries by state
#   node_crps_psu_event_total{module,kind}  per-PSU asymmetric drop events
#   node_crps_log_harvest_pages_total       pages swept (collector health)
#   node_crps_log_ring_pages                ring-buffer size last seen
#   node_crps_log_seeded                    1 once the baseline sweep ran
import glob
import hashlib
import json
import os
import re
import sys
import tempfile
import time

OUT = "/var/lib/prometheus/node-exporter/crps.prom"
# Dot-prefixed and not *.prom, so node_exporter's textfile collector
# ignores it. Holds the log-harvest seen-hashes and counters across the
# oneshot timer's separate invocations.
STATE_FILE = "/var/lib/prometheus/node-exporter/.crps-state.json"
CRPS_GLOB = "/dev/ttyACM*"

# Log entries hash-identical across a re-read are deduped; keep more keys
# than the ring holds so a full lap never re-counts an entry it already
# saw. Bounded so the state file can't grow without limit.
MAX_SEEN = 1200
# Pages to sweep per scrape. At ~240 ms/page this keeps the exclusive
# port-hold to a few seconds (the temps collector shares the bus, so the
# hold MUST stay short) while lapping a ~275-entry ring in ~14 scrapes —
# a newly-logged transient is harvested within several minutes. The
# baseline seed laps once at this same budget rather than in one long
# hold, so no single run monopolizes the port.
PAGE_BUDGET = 20
# Assumed ring size before the device first reports its own `page X/N`.
DEFAULT_RING = 275
LOG_STATES = ("normal", "alarm", "fault")

# Per-PSU sub-record layout inside a log entry: after a 21-field header,
# four 21-field module records, each idx;fan;temp1;temp2;Vin;Vout;...
MOD_BASE = 21
MOD_LEN = 21
MOD_VIN_IDX = 4
MOD_VOUT_IDX = 5
# Thresholds for "this PSU's input/output collapsed" vs "healthy".
VIN_PRESENT = 150.0   # a real AC input reading
VIN_LOST = 50.0       # effectively no input
VOUT_ON = 10.0        # delivering its ~12V rail
VOUT_OFF = 1.0        # output off
MODULE_EVENT_KINDS = ("input_loss", "output_off")

# Strip VT100 escape sequences. The controller echoes each typed
# character back wrapped in cursor-movement escapes; once those are
# removed the echo collapses to the plain command text on its own line,
# which the table/JSON parsers below simply ignore.
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip_ansi(s):
    return ANSI.sub("", s)


def _parse_float(s):
    """Parse a numeric cell, tolerating a trailing unit or hex. Returns
    None if it isn't a plain number."""
    s = s.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_int_maybe_hex(s):
    s = s.strip()
    if not s:
        return None
    try:
        if s.lower().startswith("0x"):
            return int(s, 16)
        return int(s, 10)
    except ValueError:
        return None


def parse_show_crps(text):
    """Parse a `show crps` reply into {module -> {field -> raw_str}}.

    The reply is a transposed ASCII table: columns are PSU modules
    (CRPS 1..N), rows are PMBus fields (Temp1, Temp2, Fan, Vin, Vout,
    Iin, Iout, Pin, Pout, VinType, Status, SmartOn). Returns None if no
    well-formed CRPS table is present (e.g. the reply came from the
    cooling controller, which answers "ERROR: unknown command").
    """
    text = strip_ansi(text)
    header = None  # list of module label strings, e.g. ["1", "2", ...]
    rows = {}      # field -> [cell, cell, ...] aligned to header
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.split("|")]
        # split on the bordering pipes yields empty first/last cells
        cells = [c for c in cells if c != ""]
        if not cells:
            continue
        # separator rows are all dashes
        if all(set(c) <= set("-") for c in cells):
            continue
        first = cells[0].rstrip(":")
        if header is None:
            # header row: first cell labels the table (e.g. "PMBus"),
            # the rest are module columns "CRPS N"
            mods = []
            for c in cells[1:]:
                m = re.match(r"CRPS\s*(\d+)", c, re.I)
                if not m:
                    mods = []
                    break
                mods.append(m.group(1))
            if mods:
                header = mods
            continue
        rows[first] = cells[1:]

    if not header or not rows:
        return None

    out = {}
    for i, mod in enumerate(header):
        fields = {}
        for field, cells in rows.items():
            if i < len(cells):
                fields[field] = cells[i]
        out[mod] = fields
    return out


# Alarm/fault flag rows look like:
#   |   4  |  0  | ALARM_CRPS_NORESERV        |   0   |
_FLAG_ROW = re.compile(
    r"^\|\s*\d+\s*\|\s*\d+\s*\|\s*([A-Z0-9_]+)\s*\|\s*([01])\s*\|$"
)
# Header lines: "Alarms: 00000000 (0x00)" / "Faults: 00000000 (0x00)"
_AGG_LINE = re.compile(r"^(Alarms|Faults):\s*([0-9a-fA-F]+)\s*\(0x([0-9a-fA-F]+)\)")


def parse_show_alarms(text):
    """Parse `show alarms` into (state, flags).

    state is one of "normal" | "alarm" | "fault" derived from the
    aggregate Alarms/Faults words (fault outranks alarm). flags is
    {flag_name -> 0|1} for every listed alarm and fault bit. Returns None
    if the reply has no recognizable Alarms/Faults section.
    """
    text = strip_ansi(text)
    alarms_set = faults_set = None
    flags = {}
    section_is_fault = False
    for line in text.splitlines():
        line = line.strip()
        agg = _AGG_LINE.match(line)
        if agg:
            kind, _dec, hexval = agg.groups()
            nonzero = int(hexval, 16) != 0
            if kind == "Alarms":
                alarms_set = nonzero
                section_is_fault = False
            else:
                faults_set = nonzero
                section_is_fault = True
            continue
        m = _FLAG_ROW.match(line)
        if m:
            flags[m.group(1)] = int(m.group(2))

    if alarms_set is None and faults_set is None:
        return None

    if faults_set:
        state = "fault"
    elif alarms_set:
        state = "alarm"
    else:
        state = "normal"
    return state, flags


def parse_show_params(text):
    """Parse `show params` (a single JSON line) into the redundancy
    config dict, or None if no JSON object is present."""
    text = strip_ansi(text)
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


# --- event log (`show log`) parsing and harvest accounting --------------

# A `show log` reply looks like:
#   show log\r\npage 67/275\r\n13;0;Normal;28816;4.98;...;crps>
# The data line is one long semicolon record: an id, a zero, the state,
# the epoch ts (seconds since that power-on), captured rails/temps, then
# four 21-field per-PSU records.
_LOG_PAGE = re.compile(r"page\s+(\d+)\s*/\s*(\d+)")
_LOG_STATE_TOKENS = {"normal": "normal", "alarm": "alarm", "fault": "fault"}


def parse_log_page(text):
    """Parse one `show log` reply into (page, total, dataline), or None if
    it isn't a well-formed log page (e.g. an empty/`show log N` reply)."""
    text = strip_ansi(text)
    page = total = None
    dataline = None
    for line in text.splitlines():
        line = line.strip()
        m = _LOG_PAGE.search(line)
        if m:
            page, total = int(m.group(1)), int(m.group(2))
            continue
        # the record line is semicolon-dense; the echoed "show log" and
        # the prompt are not
        if line.count(";") >= 5:
            dataline = line
    if page is None or total is None or dataline is None:
        return None
    return page, total, dataline


def parse_log_entry(dataline):
    """Extract {state, ts} from a log record line, or None. State is found
    by token (robust to leading id fields varying), ts is the integer
    immediately after it."""
    fields = [f.strip() for f in dataline.split(";")]
    for i, f in enumerate(fields):
        st = _LOG_STATE_TOKENS.get(f.lower())
        if st is None:
            continue
        ts = None
        if i + 1 < len(fields):
            try:
                ts = int(fields[i + 1])
            except ValueError:
                ts = None
        return {"state": st, "ts": ts}
    return None


def entry_key(dataline):
    """Stable content identity for a log record, independent of its
    current page position."""
    return hashlib.sha1(dataline.encode("utf-8", "replace")).hexdigest()[:16]


def parse_log_modules(dataline):
    """Return {module_str: {'vin': float, 'vout': float}} for the four
    per-PSU sub-records in a log entry, skipping any that don't parse."""
    f = dataline.split(";")
    out = {}
    for i in range(4):
        rec = f[MOD_BASE + i * MOD_LEN: MOD_BASE + i * MOD_LEN + MOD_LEN]
        if len(rec) < MOD_VOUT_IDX + 1:
            continue
        try:
            idx = int(rec[0])
            vin = float(rec[MOD_VIN_IDX])
            vout = float(rec[MOD_VOUT_IDX])
        except ValueError:
            continue
        out[str(idx)] = {"vin": vin, "vout": vout}
    return out


def module_events(dataline):
    """Per-module *asymmetric* drop events in one log entry: a module whose
    input (or output) has collapsed while at least two OTHER modules are
    healthy. That asymmetry is the signature of a single-PSU fault — a flaky
    module/connector/feed — as opposed to a bank-wide AC sag (all modules
    low, no module stands out) or a turn-on ramp (fewer than two others are
    up yet). Returns {module_str: set(kinds)}."""
    mods = parse_log_modules(dataline)
    if len(mods) < 3:
        return {}
    vin_up = [m for m, v in mods.items() if v["vin"] > VIN_PRESENT]
    vout_up = [m for m, v in mods.items() if v["vout"] > VOUT_ON]
    ev = {}
    for m, v in mods.items():
        kinds = set()
        if v["vin"] < VIN_LOST and len([x for x in vin_up if x != m]) >= 2:
            kinds.add("input_loss")
        if v["vout"] < VOUT_OFF and len([x for x in vout_up if x != m]) >= 2:
            kinds.add("output_off")
        if kinds:
            ev[m] = kinds
    return ev


def new_state():
    return {
        "seen": [],
        "counts": {s: 0 for s in LOG_STATES},
        "module_events": {},   # module_str -> {kind: count}
        "pages_total": 0,
        "ring_pages": 0,
        "seed_pages": 0,   # pages swept so far during the baseline lap
        "seeded": False,
    }


def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
    except (OSError, ValueError):
        return new_state()
    base = new_state()
    if isinstance(s, dict):
        base.update({k: s[k] for k in base if k in s and k != "counts"})
        # counts may be missing a state key across versions
        base["counts"] = {st: int(s.get("counts", {}).get(st, 0)) for st in LOG_STATES}
    return base


def save_state(state):
    d = os.path.dirname(STATE_FILE)
    fd, tmp = tempfile.mkstemp(prefix=".crps-state.", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
        os.chmod(tmp, 0o644)
        os.rename(tmp, STATE_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def harvest_entries(state, datalines, count=True):
    """Fold a batch of just-read log record lines into `state` (mutated in
    place). Already-seen content hashes are skipped; genuinely new ones are
    recorded and, when count=True, increment the per-state counters. With
    count=False the entries are only marked seen (baseline seeding), so the
    pre-existing backlog isn't replayed as fresh events. Returns the number
    of newly-seen entries."""
    seen = set(state["seen"])
    order = state["seen"]
    new = 0
    for dl in datalines:
        key = entry_key(dl)
        if key in seen:
            continue
        seen.add(key)
        order.append(key)
        new += 1
        if count:
            entry = parse_log_entry(dl)
            if entry:
                state["counts"][entry["state"]] = state["counts"].get(entry["state"], 0) + 1
            for m, kinds in module_events(dl).items():
                mc = state["module_events"].setdefault(m, {})
                for k in kinds:
                    mc[k] = mc.get(k, 0) + 1
    # bound the retained key history (drop oldest)
    if len(order) > MAX_SEEN:
        del order[:len(order) - MAX_SEEN]
    return new


def candidate_devices():
    return sorted(glob.glob(CRPS_GLOB))


def _query(port, cmd, read_timeout=1.5):
    port.reset_input_buffer()
    port.write((cmd + "\r").encode())
    port.flush()
    deadline = time.monotonic() + read_timeout
    buf = bytearray()
    while time.monotonic() < deadline:
        chunk = port.read(512)
        if chunk:
            buf.extend(chunk)
            # the prompt returns after each reply; once we've seen it the
            # controller is idle and there's nothing more to read
            if buf.rstrip().endswith(b"crps>"):
                break
    return buf.decode(errors="replace")


def _read_log_pages(port, n):
    """Advance the log cursor by up to n pages, returning (datalines,
    ring_total). Pages that don't parse are skipped but still count
    against the budget (the cursor still advanced)."""
    datalines = []
    ring_total = 0
    for _ in range(n):
        page = parse_log_page(_query(port, "show log"))
        if page is None:
            continue
        _pg, total, dl = page
        ring_total = total or ring_total
        datalines.append(dl)
    return datalines, ring_total


def read_crps(dev, retries=5, log_pages=PAGE_BUDGET):
    """Open dev and return a dict of raw replies, or None on persistent
    I/O failure or if the port isn't a CRPS controller. log_pages bounds
    how many `show log` pages to sweep this call."""
    import serial
    last_err = None
    for _ in range(retries):
        try:
            with serial.Serial(dev, 9600, timeout=0.1, exclusive=True) as port:
                crps = _query(port, "show crps")
                if parse_show_crps(crps) is None:
                    # not the CRPS controller (e.g. cooling controller)
                    return None
                alarms = _query(port, "show alarms")
                params = _query(port, "show params")
                # Harvest last so a log hiccup never costs us the gauges.
                log_lines, ring_total = _read_log_pages(port, log_pages)
            return {
                "crps": crps, "alarms": alarms, "params": params,
                "log_lines": log_lines, "ring_total": ring_total,
            }
        except (serial.SerialException, OSError) as e:
            last_err = e
            time.sleep(0.3)
    print(f"# crps read failed on {dev}: {last_err}", file=sys.stderr)
    return None


def select_crps_device(devices, reader):
    """Return (dev, replies) for the first device that answers as a CRPS
    controller, or None. `replies` is whatever reader() returned."""
    for dev in devices:
        res = reader(dev)
        if res is not None:
            return dev, res
    return None


def collect():
    """Return a metrics dict, or None if no CRPS controller is present.
    Also harvests the event log into the persistent state file."""
    try:
        import serial  # noqa: F401
    except ImportError:
        print("# pyserial not installed; skipping CRPS", file=sys.stderr)
        return None

    state = load_state()

    # Always sweep the same short page budget so the exclusive port-hold
    # stays a few seconds (the temps collector shares this bus). The
    # baseline seed is just the first full lap's worth of these short
    # sweeps: until we've lapped the ring once, entries are marked seen
    # WITHOUT counting, so the pre-existing backlog isn't replayed as
    # fresh events. After one lap, new entries count.
    selected = select_crps_device(
        candidate_devices(), lambda d: read_crps(d, log_pages=PAGE_BUDGET)
    )
    if selected is None:
        return None
    _dev, replies = selected

    metrics = build_metrics(replies["crps"], replies["alarms"], replies["params"])

    log_lines = replies.get("log_lines", [])
    if replies.get("ring_total"):
        state["ring_pages"] = replies["ring_total"]
    counting = state["seeded"]
    harvest_entries(state, log_lines, count=counting)
    state["pages_total"] = state.get("pages_total", 0) + len(log_lines)
    if not state["seeded"]:
        state["seed_pages"] = state.get("seed_pages", 0) + len(log_lines)
        ring = state["ring_pages"] or DEFAULT_RING
        if state["seed_pages"] >= ring:
            state["seeded"] = True
    save_state(state)

    metrics["log"] = {
        "counts": dict(state["counts"]),
        "module_events": {m: dict(k) for m, k in state["module_events"].items()},
        "pages_total": state["pages_total"],
        "ring_pages": state["ring_pages"],
        "seeded": state["seeded"],
    }
    return metrics


def build_metrics(crps_text, alarms_text, params_text):
    """Pure transform from the three raw replies to a metrics dict.
    Split out from collect() so it is unit-testable on canned text."""
    psus = parse_show_crps(crps_text) or {}
    modules = []
    for mod, f in psus.items():
        vout = _parse_float(f.get("Vout", ""))
        status = _parse_int_maybe_hex(f.get("Status", ""))
        # A populated slot reports a real output voltage; an empty slot
        # reads na/blank and fails the float parse -> present=0.
        present = vout is not None and vout > 0
        modules.append({
            "module":  mod,
            "vin":     _parse_float(f.get("Vin", "")),
            "vout":    vout,
            "iout":    _parse_float(f.get("Iout", "")),
            "pin":     _parse_float(f.get("Pin", "")),
            "pout":    _parse_float(f.get("Pout", "")),
            "temp1":   _parse_float(f.get("Temp1", "")),
            "temp2":   _parse_float(f.get("Temp2", "")),
            "fan":     _parse_float(f.get("Fan", "")),
            "present": present,
            # Status 0x0000 == Normal. Unknown status -> not ok.
            "ok":      present and status == 0,
        })
    modules.sort(key=lambda m: int(m["module"]) if m["module"].isdigit() else 0)

    alarms = parse_show_alarms(alarms_text)
    state, flags = alarms if alarms else (None, {})

    params = parse_show_params(params_text) or {}
    crps_cfg = params.get("crps", {}) if isinstance(params, dict) else {}

    return {
        "modules": modules,
        "state": state,
        "flags": flags,
        "work_qty": crps_cfg.get("workQty"),
        "reserve_qty": crps_cfg.get("reserveQty"),
        "redundancy_enabled": crps_cfg.get("crEnable"),
    }


def esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


def _g(lines, name, help_text):
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} gauge")


def write_prom(metrics):
    lines = []
    mods = metrics["modules"] if metrics else []

    def emit_psu(name, help_text, key, extra_labels=None):
        _g(lines, name, help_text)
        for m in mods:
            v = m[key]
            if v is None:
                continue
            labels = f'module="{esc(m["module"])}"'
            if extra_labels:
                labels += "," + extra_labels
            lines.append(f"{name}{{{labels}}} {v}")

    emit_psu("node_crps_psu_vin_volts", "CRPS PSU input voltage (V).", "vin")
    emit_psu("node_crps_psu_vout_volts", "CRPS PSU output voltage (V).", "vout")
    emit_psu("node_crps_psu_iout_amps", "CRPS PSU output current (A).", "iout")
    emit_psu("node_crps_psu_pin_watts", "CRPS PSU input power (W).", "pin")
    emit_psu("node_crps_psu_pout_watts", "CRPS PSU output power (W).", "pout")

    _g(lines, "node_crps_psu_temp_celsius", "CRPS PSU temperature probe (C).")
    for m in mods:
        for probe, key in (("1", "temp1"), ("2", "temp2")):
            v = m[key]
            if v is None:
                continue
            lines.append(
                f'node_crps_psu_temp_celsius{{module="{esc(m["module"])}",probe="{probe}"}} {v}'
            )

    emit_psu("node_crps_psu_fan_rpm", "CRPS PSU fan tachometer (RPM).", "fan")

    _g(lines, "node_crps_psu_present", "1 = CRPS PSU module enumerated.")
    for m in mods:
        lines.append(f'node_crps_psu_present{{module="{esc(m["module"])}"}} {int(m["present"])}')

    _g(lines, "node_crps_psu_ok", "1 = CRPS PSU module in Normal state.")
    for m in mods:
        lines.append(f'node_crps_psu_ok{{module="{esc(m["module"])}"}} {int(m["ok"])}')

    # Controller aggregate state, 1-hot.
    state = metrics["state"] if metrics else None
    _g(lines, "node_crps_state", "Controller aggregate state, 1-hot (normal|alarm|fault).")
    for s in ("normal", "alarm", "fault"):
        lines.append(f'node_crps_state{{state="{s}"}} {1 if state == s else 0}')

    # Per-flag alarm/fault bits.
    flags = metrics["flags"] if metrics else {}
    _g(lines, "node_crps_alarm", "CRPS active alarm/fault flag (1 = set).")
    for flag, val in sorted(flags.items()):
        lines.append(f'node_crps_alarm{{flag="{esc(flag)}"}} {int(val)}')

    def emit_scalar(name, help_text, value, cast=int):
        _g(lines, name, help_text)
        if value is not None:
            lines.append(f"{name} {cast(value)}")

    emit_scalar("node_crps_work_qty", "Configured Work (active) PSU modules.",
                metrics["work_qty"] if metrics else None)
    emit_scalar("node_crps_reserve_qty", "Configured Reserve (hot-spare) PSU modules.",
                metrics["reserve_qty"] if metrics else None)
    emit_scalar("node_crps_redundancy_enabled", "Cold-redundancy (crEnable) configured.",
                metrics["redundancy_enabled"] if metrics else None,
                cast=lambda b: 1 if b else 0)

    # Event-log harvest. Counters are cumulative across the oneshot timer's
    # runs (persisted in STATE_FILE); a logged transient the live gauges
    # missed shows as increase(node_crps_log_event_total{state="fault"}).
    log = metrics.get("log") if metrics else None
    lines.append("# HELP node_crps_log_event_total Distinct CRPS event-log entries harvested, by state.")
    lines.append("# TYPE node_crps_log_event_total counter")
    if log:
        for st in LOG_STATES:
            lines.append(f'node_crps_log_event_total{{state="{st}"}} {int(log["counts"].get(st, 0))}')
    # Per-PSU asymmetric drop events harvested from the log: a single
    # module losing input/output while the others hold steady — the
    # dropout-asymmetry signal that fingers a specific flaky PSU/feed
    # rather than a bank-wide sag.
    me = log["module_events"] if log else {}
    lines.append("# HELP node_crps_psu_event_total Per-PSU asymmetric drop events from the event log (single module down while >=2 others healthy).")
    lines.append("# TYPE node_crps_psu_event_total counter")
    for m in sorted(me, key=lambda x: int(x) if x.isdigit() else 0):
        for kind in MODULE_EVENT_KINDS:
            lines.append(
                f'node_crps_psu_event_total{{module="{esc(m)}",kind="{kind}"}} {int(me[m].get(kind, 0))}'
            )
    lines.append("# HELP node_crps_log_harvest_pages_total Cumulative `show log` pages swept by the collector.")
    lines.append("# TYPE node_crps_log_harvest_pages_total counter")
    if log:
        lines.append(f"node_crps_log_harvest_pages_total {int(log['pages_total'])}")
    _g(lines, "node_crps_log_ring_pages", "CRPS event-log ring-buffer size (entries).")
    if log and log.get("ring_pages"):
        lines.append(f"node_crps_log_ring_pages {int(log['ring_pages'])}")
    _g(lines, "node_crps_log_seeded", "1 once the baseline log sweep has run.")
    if log:
        lines.append(f"node_crps_log_seeded {1 if log['seeded'] else 0}")

    body = "\n".join(lines) + "\n"

    d = os.path.dirname(OUT)
    fd, tmp = tempfile.mkstemp(prefix=".crps.", suffix=".prom", dir=d)
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
    metrics = collect()
    # Always rewrite the file (even when no controller is found, yielding
    # an empty body of HELP/TYPE headers) so node_textfile_mtime_seconds
    # advances and the staleness alert keys only on the script stopping —
    # same discipline as node-temps-collector.
    write_prom(metrics)
    return 0


if __name__ == "__main__":
    sys.exit(main())
