"""Pure helpers shared by the admin app's health, scheduling, and usage views."""
from datetime import datetime, UTC

from health_common import (_add_health_service, _required_service_health,
                           _standby_service_health)

def _gateway_vip_health(vip_health):
    active = vip_health['gateway_vip']['active']
    return {'status': 'healthy' if active else 'standby', 'details': vip_health['gateway_vip']}

def _storage_vip_health(vip_health):
    active = vip_health['storage_vip']['active']
    return {'status': 'healthy' if active else 'standby', 'details': vip_health['storage_vip']}

def _can_edit_vm(id, rec, desired):
    """Whether the caller may change this VM's mode via
    /admin/vm-schedule/set. Encodes the same rules the POST endpoint
    enforces, so the schedule page can disable the controls in JS
    without re-deriving them. Returned in each row's `can_edit` field;
    the template binds `:disabled="!row.can_edit"` to the mode <select>
    + Save button. Mirrors vm_schedule_set:
      - admin: yes (any VM)
      - non-admin owner (manager or user): yes, unless admin-set
        unmanaged
      - non-admin non-owner (manager or user): no — managers see other
        users' VMs but can't edit them; the page filters them out via
        the ownership check below.
    Note: an owner can't select the 'unmanaged' option itself (admin
    escape hatch) — handled by the option's `:disabled="!isAdmin"`."""
    if id.is_admin:
        return True
    if rec.get('owner') != id.email:
        return False
    return not (desired is not None and desired.get('mode') == 'unmanaged')

def _parse_windows(windows):
    """Validate one-shot scheduling windows and normalize them to UTC ISO
    strings, sorted, with fully-elapsed windows dropped. Each window is
    {'start': <ISO datetime>, 'end': <ISO datetime>} with explicit
    timezones (the page sends UTC). Returns the normalized list, or None
    if anything is invalid."""
    if not isinstance(windows, list) or len(windows) > 50:
        return None
    now = datetime.now(UTC)
    out = []
    for w in windows:
        if not isinstance(w, dict):
            return None
        try:
            start = datetime.fromisoformat(str(w.get('start')))
            end = datetime.fromisoformat(str(w.get('end')))
        except (TypeError, ValueError):
            return None
        if start.tzinfo is None or end.tzinfo is None:
            return None
        start, end = start.astimezone(UTC), end.astimezone(UTC)
        if end <= start:
            return None
        if end <= now:
            continue
        out.append({'start': start.isoformat(timespec='seconds'),
                    'end': end.isoformat(timespec='seconds')})
    return sorted(out, key=lambda w: w['start'])

def _assemble_intervals(events, since_dt, now):
    """Assemble running intervals per VM from the event stream (each VM's
    pre-window event sorts before its window events, and the state machine
    is per-VM, so the combined list needs no global re-sort). launch/start
    open an interval (restart keeps one open across the gap), stop/destroy
    close it; an interval still open accrues until now. Time is clipped to
    the window; billable is a property of the opening event.

    Returns {vm: {'owner': str, 'open': (ts, gpus, billable) | None,
                  'billable': float, 'tracked': float}}.
    """
    def accrue(st, end):
        start, ev_gpus, ev_billable = st['open']
        lo, hi = max(start, since_dt), min(end, now)
        if hi > lo:
            hours = (hi - lo).total_seconds() / 3600 * (ev_gpus or 0)
            st['tracked'] += hours
            if ev_billable:
                st['billable'] += hours

    per_vm = {}
    for ts, vm, event, owner, gpus, billable in events:
        st = per_vm.setdefault(vm, {'owner': owner, 'open': None,
                                    'billable': 0.0, 'tracked': 0.0})
        if owner:
            st['owner'] = owner
        if event in ('launch', 'start', 'restart'):
            if st['open'] is None:
                st['open'] = (ts, gpus, billable)
        elif event in ('stop', 'destroy'):
            if st['open'] is not None:
                accrue(st, ts)
                st['open'] = None
    for st in per_vm.values():
        if st['open'] is not None:
            accrue(st, now)
    return per_vm

def _usage_row(vm, ev, sm, registry, desired_all, last_state):
    """Render one row of the per-VM usage table."""
    owner = ev.get('owner') or sm.get('owner') or ''
    tracked = round(ev.get('tracked', 0.0), 4)
    observed = round(sm.get('observed', 0.0), 4)
    # Is the VM on the clock right now, and on whose dime: an open
    # event interval says billable/unbilled; running per the latest
    # sample with no open interval is untracked runtime.
    if ev.get('open') is not None:
        current = 'billable' if ev['open'][2] else 'unbilled'
    elif last_state.get(vm) == 'Running':
        current = 'untracked'
    else:
        current = None
    return {
        'vm': vm,
        'owner': owner,
        'gpus': registry.get(vm, {}).get('gpus'),
        'schedule_mode': ((desired_all.get(vm) or {}).get('mode', 'unmanaged')
                          if vm in registry else None),
        'windows': (desired_all.get(vm) or {}).get('windows', []),
        'billable_gpu_hours': round(ev.get('billable', 0.0), 4),
        'tracked_gpu_hours': tracked,
        'observed_gpu_hours': observed,
        'samples': sm.get('samples', 0),
        'current': current,
        # Sampling granularity is one interval; flag drift beyond that
        # plus 10% slack.
        'drift': abs(observed - tracked) > max(0.1, tracked * 0.1),
    }
