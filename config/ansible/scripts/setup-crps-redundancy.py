#!/usr/bin/env python3
"""Configure the Comino CRPS power-supply controller's redundancy mode.

Targets the `crps>` STM32 USB-CDC device that aggregates per-PSU telemetry
over PMBus on Comino workstation-class GPU chassis. See
docs/design/comino-crps-power-monitoring.md for the protocol and
docs/design/comino-crps-redundancy-setup.md (TODO) for the rationale.

This script is the *write* counterpart to the read-only
config/ansible/monitoring/files/crps-collector.py — it issues `setup` to
set workQty / reserveQty / crEnable. The collector deliberately never calls
setup; this script runs at provisioning time only.

Protocol: 9600 8N1, prompt `crps>`, `cmd\r` in, read until the prompt
returns. Strip VT100 escapes. Same `exclusive=True` / EBUSY retry pattern
as the collector (the temps collector shares the bus — keep the port-hold
short).

Args:
    --work-qty N        Number of active PSUs (default 3, e.g. 3 for 3+1)
    --reserve-qty N     Number of reserve PSUs (default 1, e.g. 1 for 3+1)
    --cr-enable BOOL    Enable CR (cold redundancy) mode (default true)
    --probe-only        Read current params and exit (don't write)
    --retries N         EBUSY + wizard retry count (default 5)

Exit codes:
    0  already configured OR successfully configured
    1  failed (setup returned error or verification mismatch)
    2  no CRPS controller found on this host (skip silently)

The script prints one structured status line per phase on stdout
(`PHASE=... STATUS=...`) so ansible's `command` module can `register:` it
and the playbook can branch on the result without parsing free text.
"""
import argparse
import glob
import json
import os
import re
import sys
import time

import serial

CRPS_GLOB = "/dev/ttyACM*"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def log(phase, status, **kv):
    """Emit one structured status line. ansible parses these with
    `register:` + `with_items` if it needs them; humans can grep."""
    parts = [f"PHASE={phase}", f"STATUS={status}"]
    for k, v in sorted(kv.items()):
        parts.append(f"{k}={v}")
    print(" ".join(parts), flush=True)


def strip_ansi(s):
    return ANSI.sub("", s)


def _query(port, cmd, read_timeout=1.5):
    """Send `cmd\r`, read until the `crps>` prompt returns."""
    port.reset_input_buffer()
    port.write((cmd + "\r").encode())
    port.flush()
    deadline = time.monotonic() + read_timeout
    buf = bytearray()
    while time.monotonic() < deadline:
        chunk = port.read(512)
        if chunk:
            buf.extend(chunk)
            if buf.rstrip().endswith(b"crps>"):
                break
    return buf.decode(errors="replace")


def _reset_wizard(port, timeout=2.0):
    """Send Ctrl-C to break out of the CRPS setup wizard (or any input
    prompt) and return to the normal `crps>` shell. The controller
    persists wizard state across reconnects, so this is the first thing
    every probe does — if the wizard is open from a prior manual `setup`
    session, all subsequent `show` commands would be parsed as wizard
    input and rejected. Returns True if `crps>` appears in the reply,
    False if the controller didn't return to the normal shell.
    A no-op when the controller is already at `crps>`."""
    port.reset_input_buffer()
    port.write(b"\x03")  # Ctrl-C, no \r — interpreted as soon as received
    port.flush()
    deadline = time.monotonic() + timeout
    buf = bytearray()
    while time.monotonic() < deadline:
        chunk = port.read(512)
        if chunk:
            buf.extend(chunk)
            if buf.rstrip().endswith(b"crps>"):
                return True
    return False


def parse_show_params(text):
    """Return the JSON dict from `show params`, or None."""
    text = strip_ansi(text)
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def parse_show_crps_modules(text):
    """Return the list of CRPS module numbers from `show crps`, or None
    if the reply isn't from a CRPS controller."""
    text = strip_ansi(text)
    mods = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.split("|") if c.strip()]
        if not cells:
            continue
        if all(set(c) <= set("-") for c in cells):
            continue
        # header row: first cell labels the table, rest are module columns
        if not mods:
            for c in cells[1:]:
                m = re.match(r"CRPS\s*(\d+)", c, re.I)
                if not m:
                    mods = []
                    break
                mods.append(int(m.group(1)))
    return mods or None


def open_crps(dev, retries=5):
    """Open dev with TIOCEXCL and return (params, modules, dev) — or None if
    the device isn't a CRPS controller, the port can't be opened, or the
    controller is wedged in the setup wizard and Ctrl-C recovery didn't
    bring it back to the `crps>` prompt.

    Every open starts with a Ctrl-C reset: the controller persists wizard
    state across reconnects, and without it every query would be parsed as
    wizard input. A no-op when already at the prompt; logged either way so
    the operator can see when recovery actually kicked in."""
    for attempt in range(retries):
        try:
            with serial.Serial(dev, 9600, timeout=0.1, exclusive=True) as port:
                if not _reset_wizard(port):
                    log("probe", "wizard_reset_failed", dev=dev, attempt=attempt + 1)
                    return None
                log("probe", "wizard_reset_ok", dev=dev, attempt=attempt + 1)
                crps_text = _query(port, "show crps")
                if parse_show_crps_modules(crps_text) is None:
                    return None
                params_text = _query(port, "show params")
                params = parse_show_params(params_text)
                if params is None:
                    return None
                # Open a second exclusive handle to issue `setup` — the
                # `with` block above closes the port on return, so we
                # return the params and let the caller re-open.
                return params, parse_show_crps_modules(crps_text), dev
        except (serial.SerialException, OSError) as e:
            log("probe", "ebusy", dev=dev, attempt=attempt + 1, err=str(e))
            time.sleep(0.3)
    return None


def select_crps_device(retries=5):
    """Return (params, modules, dev) for the first /dev/ttyACM* that
    answers as a CRPS controller, or None."""
    for dev in sorted(glob.glob(CRPS_GLOB)):
        res = open_crps(dev, retries=retries)
        if res is not None:
            return res
    return None


def apply_setup(dev, args, modules):
    """Open dev, walk the `setup crps` wizard step by step with the
    desired config, return the new params dict on success, or None on
    failure.

    Wizard flow (captured from the standard Comino STM32 CRPS firmware;
    may shift on other builds — the script logs every step so a firmware
    change is easy to diagnose):
        1. `setup crps` -> `"Work" [1 - 4] =`     -> answer workQty
        2.              -> `"Reserve" [0 - 1] =`  -> answer reserveQty
        3.              -> `Activate? [y/n]:`     -> answer y/n
        4.              -> `Selected module(s) =` -> answer module number(s)
           (only when step 3 was y; comma-separated for multiple)
        5.              -> `Save? [y/N]:`         -> answer y to commit
    The wizard closes on either branch of step 5 and returns to `crps>`.

    The reserve module defaults to the highest-numbered module (the
    spare slot on a typical Comino chassis). Override per-host via
    `comino_crps_reserve_module` in the playbook if needed."""
    # Default reserve module: highest-numbered module. On a 4-PSU
    # chassis that's module 4, the spare slot.
    reserve_module = max(modules) if modules else args.work_qty + args.reserve_qty
    steps = [
        ("workQty",        str(args.work_qty),    2.5),
        ("reserveQty",     str(args.reserve_qty), 2.5),
        ("crEnable",       "y" if args.cr_enable == "true" else "n", 2.5),
        ("reserve_module", str(reserve_module),   2.5),
        ("save",           "y",                   4.0),
    ]
    if args.cr_enable != "true":
        # Step 4 only fires when CR is on — the firmware doesn't ask
        # which module to put in cold standby otherwise.
        steps = [s for s in steps if s[0] != "reserve_module"]

    for attempt in range(args.retries):
        try:
            with serial.Serial(dev, 9600, timeout=0.1, exclusive=True) as port:
                # Same as open_crps: a prior partial setup may have left
                # the wizard open. Reset before opening a fresh one so we
                # don't accidentally feed wizard input.
                if not _reset_wizard(port):
                    log("setup", "wizard_reset_failed", dev=dev, attempt=attempt + 1)
                    return None
                # Sanity: confirm the device still answers as CRPS.
                if parse_show_crps_modules(_query(port, "show crps")) is None:
                    return None
                log("setup", "wizard_open", dev=dev, attempt=attempt + 1,
                    workQty=args.work_qty, reserveQty=args.reserve_qty,
                    crEnable=args.cr_enable, reserve_module=reserve_module)
                # Step 0: open the wizard.
                reply = strip_ansi(_query(port, "setup crps", read_timeout=2.5))
                if "Work" not in reply:
                    log("setup", "wizard_open_failed", dev=dev,
                        attempt=attempt + 1,
                        reply_tail=reply.replace("\r", " ").strip()[-120:])
                    continue
                # Walk each step.
                walk_ok = True
                for step_name, value, step_timeout in steps:
                    reply = strip_ansi(_query(port, value, read_timeout=step_timeout))
                    log("setup", "wizard_step", step=step_name, value=value,
                        attempt=attempt + 1,
                        reply_tail=reply.replace("\r", " ").strip()[-80:])
                    # Rejection markers from the firmware.
                    if "Invalid value entered" in reply or "Not found" in reply:
                        log("setup", "wizard_step_rejected", step=step_name,
                            value=value, attempt=attempt + 1)
                        walk_ok = False
                        break
                if not walk_ok:
                    continue
                # Verify. Belt + braces: reset before `show params` in case
                # the wizard left us in a weird state on reject.
                _reset_wizard(port)
                verify = parse_show_params(_query(port, "show params"))
                if verify is None:
                    log("setup", "verify_parse_failed", dev=dev, attempt=attempt + 1)
                    continue
                return verify
        except (serial.SerialException, OSError) as e:
            log("setup", "ebusy", dev=dev, attempt=attempt + 1, err=str(e))
            time.sleep(0.3)
    return None


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    # Defaults match the standard 3+1 config, so `--probe-only` is
    # runnable on its own (operator can probe without committing to a
    # specific qty — current params are reported regardless).
    p.add_argument("--work-qty", type=int, default=3)
    p.add_argument("--reserve-qty", type=int, default=1)
    p.add_argument("--cr-enable", type=str, default="true",
                   choices=["true", "false"])
    p.add_argument("--probe-only", action="store_true")
    p.add_argument("--retries", type=int, default=5)
    args = p.parse_args()

    selected = select_crps_device(retries=args.retries)
    if selected is None:
        log("discover", "no_controller")
        sys.exit(2)

    params, modules, dev = selected
    crps_cfg = params.get("crps", {})
    cur_work = crps_cfg.get("workQty")
    cur_reserve = crps_cfg.get("reserveQty")
    cur_cr = crps_cfg.get("crEnable")
    log("discover", "found", dev=dev, modules=",".join(str(m) for m in modules),
        cur_workQty=cur_work, cur_reserveQty=cur_reserve,
        cur_crEnable=cur_cr)

    total_psu = len(modules)
    desired_total = args.work_qty + args.reserve_qty
    if desired_total != total_psu:
        log("validate", "psu_count_mismatch",
            desired_total=desired_total, actual_psu_count=total_psu)
        sys.exit(1)

    desired = {
        "workQty": args.work_qty,
        "reserveQty": args.reserve_qty,
        "crEnable": args.cr_enable == "true",
    }
    already = (cur_work == desired["workQty"]
               and cur_reserve == desired["reserveQty"]
               and cur_cr == desired["crEnable"])
    if args.probe_only:
        log("probe", "ok", already_configured=already)
        sys.exit(0)
    if already:
        log("setup", "noop", reason="already_at_desired_config")
        sys.exit(0)

    verify = apply_setup(dev, args, modules)
    if verify is None:
        log("setup", "failed")
        sys.exit(1)
    new_cfg = verify.get("crps", {})
    ok = (new_cfg.get("workQty") == desired["workQty"]
          and new_cfg.get("reserveQty") == desired["reserveQty"]
          and new_cfg.get("crEnable") == desired["crEnable"])
    if not ok:
        log("setup", "verify_mismatch",
            expected_work=desired["workQty"],
            actual_work=new_cfg.get("workQty"),
            expected_reserve=desired["reserveQty"],
            actual_reserve=new_cfg.get("reserveQty"))
        sys.exit(1)
    log("setup", "ok",
        workQty=new_cfg.get("workQty"),
        reserveQty=new_cfg.get("reserveQty"),
        crEnable=new_cfg.get("crEnable"))


if __name__ == "__main__":
    main()
