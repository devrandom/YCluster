# Comino CRPS power-supply monitoring

## Motivation

On Comino workstation-class GPU chassis the BMC exposes **no usable PSU
telemetry** — PSU sensors read `na`, and the DCMI power reading is broken (0 W).
The only real source of per-PSU input voltage, output power, and fault state is
the **Comino CRPS controller**: a second STM32 USB-CDC device that aggregates the
chassis power supplies (e.g. 4× Great Wall CRPS2000D2) over PMBus.

We want this telemetry in Prometheus so that AC sags, single-PSU dropouts, and
the controller's redundancy decisions are visible with real wall-clock
timestamps and can drive alerts — instead of being reconstructed by hand from
the controller's on-board event log after an incident.

This mirrors the existing `node-temps-collector` (see
`config/ansible/monitoring/files/node-temps-collector.py` and
`install-node-temps.yml`): a Prometheus **textfile collector** run from a short
systemd timer, not a long-running exporter.

## The device

Two distinct STM32 USB-CDC controllers can appear on a Comino chassis, both as
`/dev/ttyACM*`:

| Controller            | Protocol                | Purpose                          | Reader                          |
|-----------------------|-------------------------|----------------------------------|---------------------------------|
| RM cooling controller | `:get_data\r` → CSV     | coolant/air temps, fans, pumps   | `contrib/comino-sensors.py`     |
| **CRPS power controller** | `crps>` line shell  | per-PSU Vin/Vout/Iin/Iout/P/temp/fan, alarms, redundancy config | **this collector** |

Enumeration order is **not** fixed: the CRPS controller may take `ttyACM0` and
push the cooling controller to `ttyACM1`, or vice versa. Both collectors must
**auto-discover** their device by probing each `/dev/ttyACM*` for a well-formed
reply, never by hardcoding a node. The CRPS controller is identified by a
`crps>` prompt and a parseable `show crps` reply; the cooling controller by a
24-field `:get_data` reply.

### CRPS line protocol

9600 8N1, prompt `crps>`. Send `cmd\r`, read to idle/`\r\n`, strip ANSI escapes.
**Read-only commands only** — never `reboot`, `restore`, `backup`, or `setup`
(those write config or reset the controller).

Real reply formats (captured from a 4× Great Wall CRPS2000D2 chassis):

- `show crps` — a **transposed** ASCII table: columns are PSU modules
  (`CRPS 1..N`), rows are PMBus fields (`Temp1 Temp2 Fan Vin Vout Iin Iout Pin
  Pout VinType Status SmartOn`). `Status: 0x0000` == Normal.
- `show alarms` — two labelled bit tables (`Alarms:`/`Faults:`) with an
  aggregate hex word and per-flag `State` column (e.g. `ALARM_CRPS_NORESERV`,
  `FAULT_OVERHEAT`).
- `show params` — a single **JSON** line:
  `{"temp":{...},"crps":{"workQty":2,"reserveQty":2,"crEnable":false,...},"combineMode":false}`.
- `show log` — on-board event ring buffer. Each entry is one long
  semicolon record: `id;0;<state>;<ts>;` + rails + temps + four 21-field
  per-PSU sub-records (`idx;fan;temp1;temp2;Vin;Vout;Iin;Iout;Pout;Pin;…`).
  **Stateful**: each call shows `page X/N` and advances the cursor by one
  (the cursor persists across reconnects and wraps at the ring size `N`);
  there is **no random access** (`show log <n>` returns nothing). Entries
  span multiple power-on epochs, so `ts` is **not globally monotonic**.

The controller echoes each typed character back wrapped in VT100 cursor
escapes; strip ANSI before parsing and the echo collapses to a plain line
the parsers ignore.

## Serial-port contention

Both collectors probe the same `/dev/ttyACM*` bus, and the CRPS log is
**stateful** (paging advances a cursor). Rules:

- Open with `exclusive=True` (TIOCEXCL) and retry on `EBUSY`, exactly as
  `comino-sensors.py` / `node-temps-collector.py` do.
- The CRPS collector and the temps collector should **not run on the same tick**.
  Stagger the timers, and keep each collector's port-hold as short as possible.
- Treat any probe that returns the *other* controller's signature as "not my
  device" and move on — never issue CRPS write/paging commands to a port that
  answered `:get_data`, and vice versa.

## Metrics

Steady-state gauges, one series per PSU module (`module="1".."N"`):

```
node_crps_psu_vin_volts{module}            input voltage
node_crps_psu_vout_volts{module}           output voltage
node_crps_psu_iout_amps{module}            output current
node_crps_psu_pin_watts{module}            input power
node_crps_psu_pout_watts{module}           output power
node_crps_psu_temp_celsius{module,probe}   PSU temp probes
node_crps_psu_fan_rpm{module}              PSU fan
node_crps_psu_present{module}              1 = module enumerated
node_crps_psu_ok{module}                   1 = module in Normal state
```

Controller-level:

```
node_crps_state{state="normal|alarm|fault"}  1-hot current aggregate state
node_crps_alarm{flag="..."}                  active alarm/fault flags (from show alarms)
node_crps_work_qty                           configured Work modules
node_crps_reserve_qty                        configured Reserve (hot-spare) modules
node_crps_redundancy_enabled                 crEnable (cold redundancy)
node_crps_epoch_seconds                      current power-on epoch age (see ts below)
```

Atomic write to the node-exporter textfile dir
(`/var/lib/prometheus/node-exporter/crps.prom`), always rewriting so
`node_textfile_mtime_seconds` advances and a staleness alert fires if the
collector stops — same discipline as `node-temps-collector`.

## Sampling rate and what the live poll can/can't catch

A timer-driven poll (10–30 s) captures **steady-state and slow trends**: idle
draw, a PSU that stays dropped, a redundancy config change, rising temps.

It will **miss sub-second transients** — an AC sag or a momentary single-PSU
dropout happens and recovers between scrapes. Those are *not* lost: the CRPS
controller logs every `Normal→Fault→Alarm` transition to its on-board event ring
buffer. So there are **two data paths**, and the collector feeds both:

1. **Live gauges** (above) — current state, for dashboards and steady-state
   alerts.
2. **Event harvest** — the collector tails new `show log` entries and republishes
   them as timestamped events / counters, so fast transients the poll missed are
   still recorded with a wall-clock time.

## The event log and `ts` → wall-clock

The on-board log entry carries a `ts` field. **`ts` is seconds since the
controller last powered on, and resets to 0 on every controller power-on** (i.e.
on every AC restore — the controller runs on AC, so host reboots that keep AC do
*not* reset it; a full AC cut does). The log is therefore a *series of
per-power-on epochs*, not one monotonic counter: turn-on ramps appear as
`ts=3,5,6,7…` entries right after each AC restore.

### Decision: anchor only the current epoch

A past epoch's `ts=0` is the moment the **controller** powered on. That is **not
reliably a host boot** — on a given AC restore the host may sit in BIOS, fail to
POST, or never boot at all — so we **cannot** align historical epochs to the host
boot log. Attempting it would silently mis-date events.

Therefore:

- **The collector is the wall-clock source of truth.** It runs continuously and
  stamps each newly-observed event with its *own* scrape time. No `ts` decoding
  is needed for live operation.
- **`ts` is used only within the current epoch**, to interpolate events that
  occurred *between* two scrapes: with the live epoch age `ts_now` read at scrape
  time `T_wall`, an unharvested current-epoch entry at `ts_e` maps to
  `T_wall − (ts_now − ts_e)`.
- **An epoch reset is itself an event**: when `ts` jumps downward between scrapes,
  the controller power-cycled — emit a counter (`node_crps_power_on_total`) and
  reset the in-collector anchor.
- **Pre-collector / historical epochs are sequence-only** — readable for ordering
  and counts (e.g. "module 4 dropped out N times"), but **not** wall-clock
  datable. The collector does not attempt to backfill them.

This keeps every exported timestamp honest: it is either the collector's own
observation time, or a current-epoch interpolation off a live anchor — never a
guess about when a historical power-on lined up with a boot.

## Redundancy config as monitored state

The `workQty`/`reserveQty`/`crEnable` configuration determines whether losing one
PSU is a tolerated *alarm* or triggers a *poweroff*. A non-redundant
`workQty=N, reserveQty=0` bank powers the box off the instant any module drops —
turning a momentary single-PSU blip into a host shutdown. Exporting the config as
metrics lets an alert fire if the bank is ever left in a non-redundant state, and
lets dashboards correlate a poweroff with the redundancy posture at the time.

## Alerting (sketch)

- **PSU dropout**: `node_crps_psu_present == 0` for any module, or `node_crps_psu_ok == 0`.
- **Dropout asymmetry**: one module accounting for a disproportionate share of
  dropout events points at a specific flaky PSU/connector rather than an AC-side
  problem — worth a recording rule over `node_crps_psu_present` flap counts.
- **AC sag**: `node_crps_psu_vin_volts` below threshold on **multiple** modules at
  once (a single module ramping is a benign turn-on, not a sag).
- **Non-redundant config**: `node_crps_reserve_qty == 0` (or redundancy disabled).
- **Collector stale**: `node_textfile_mtime_seconds` for `crps.prom` too old.

## Deployment

Installed on nvidia nodes that have a CRPS controller, gated on its presence
(auto-discovery returns nothing → emit no series, like the temps collector's
optional Comino path). Add to `config/ansible/monitoring/` alongside the temps
collector — either as a second collector script under one timer, or its own
short timer staggered against the temps timer to avoid bus contention. Unit
tests follow the `test_node_temps_collector.py` pattern: feed canned `show crps`
/ `show alarms` / `show log` text to the parsers and assert the emitted series.

## Implementation notes (as built)

The harvest does not depend on `ts` ordering or cursor position. Each log
entry is identified by a **content hash**; the collector sweeps a short,
fixed page budget per scrape (keeping the exclusive port-hold to a few
seconds — the temps collector shares the bus), dedups against a bounded
set of seen hashes, and accumulates **persistent counters** in a small
state file (`/var/lib/prometheus/node-exporter/.crps-state.json`) so they
survive the oneshot timer's invocations. The first lap of the ring is a
**baseline seed**: entries are marked seen *without* counting, so the
pre-existing backlog isn't replayed as fresh events on startup. The seed
is spread across the first ~14 short sweeps rather than one long
port-monopolising read. Harvested entries become
`node_crps_log_event_total{state}`; a logged fault that the live gauges
missed surfaces as `increase(...{state="fault"}[15m]) > 0`.

## Open questions / resolved

- **Live "current epoch seconds" counter** — the firmware exposes no clean
  one (`show worktime` is hour-bucketed). The collector therefore does not
  emit `node_crps_epoch_seconds`; the counters' Prometheus ingestion time
  is the wall clock, which is what the design's "collector is the
  wall-clock source of truth" actually requires. Per-epoch `ts`
  interpolation is left unimplemented (cross-epoch `ts` is non-monotonic
  and not needed for the counter path).
- **Ring depth vs. poll** — ring is `N≈275` entries. The short-budget
  sweep laps it every few minutes; under a sustained fault storm
  generating >N entries faster than a lap, the oldest unharvested entries
  can be overwritten before they're counted. The live gauge + counter
  still record that a storm is in progress; the bounded loss is the tail
  of a storm's history, not its existence. (Documented, not silently
  capped.)
- **Per-module dropout-asymmetry counters** — the per-PSU sub-records are
  parsed but not yet emitted as per-module log counters; the bank-level
  `state` counters cover the headline "a transient happened" need.
- **`show params` temp limits** for a "thermal limit changed" alert —
  still optional; not exported.
