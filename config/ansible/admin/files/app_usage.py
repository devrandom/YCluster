"""Helpers for the per-VM GPU usage endpoint."""

def fetch_usage_state(cur, since_dt, until_dt=None):
    """Read lifecycle events, sample aggregates, and latest VM state.

    until_dt bounds the sample aggregation (year windows need a hard end);
    None leaves it open-ended for day windows ending at "now"."""
    # The latest pre-window event decides whether an interval was open (and
    # with what gpus/billable) at the window start, bounding the scan.
    cur.execute('''SELECT DISTINCT ON (vm) ts, vm, event, owner, gpus, billable
                   FROM vm_events WHERE ts < %s
                   ORDER BY vm, ts DESC''', [since_dt])
    pre_events = cur.fetchall()
    cur.execute('''SELECT ts, vm, event, owner, gpus, billable
                   FROM vm_events WHERE ts >= %s
                   ORDER BY vm, ts''', [since_dt])
    events = pre_events + cur.fetchall()

    sample_where = 'WHERE ts >= %s' if until_dt is None else 'WHERE ts >= %s AND ts < %s'
    sample_params = [since_dt] if until_dt is None else [since_dt, until_dt]
    cur.execute(f'''
        SELECT vm, MAX(COALESCE(owner, '')),
               SUM(CASE WHEN state = 'Running' THEN gpus * interval_s ELSE 0 END)::float / 3600,
               COUNT(*) FILTER (WHERE state = 'Running')
        FROM vm_samples {sample_where}
        GROUP BY vm
    ''', sample_params)
    sampled = {r[0]: {'owner': r[1], 'observed': r[2] or 0.0,
                      'samples': r[3]} for r in cur.fetchall()}
    cur.execute('''SELECT DISTINCT ON (vm) vm, state
                   FROM vm_samples ORDER BY vm, ts DESC''')
    return events, sampled, dict(cur.fetchall())


def registry_snapshots(vmm):
    """Best-effort registry and schedule snapshots for usage rendering."""
    try:
        return vmm.vms_all(), vmm.desired_all()
    except Exception:
        return {}, {}


def owner_visibility(identity, authentik_manager):
    """Return an owner filter for the caller's role.

    Managers see only owners in their authorized projects; admins and plain
    users see all rows (plain users are scoped by the pages feeding this API).
    """
    if not (identity.is_manager and not identity.is_admin):
        return lambda owner: True

    managed = {p for p in authentik_manager.manager_projects(identity.email)
               if p}
    cache = {}

    def visible(owner):
        if owner not in cache:
            try:
                project = authentik_manager.user_project(owner) if owner else ''
            except Exception:
                project = ''
            cache[owner] = project
        return cache[owner] in managed

    return visible


def usage_rows(per_vm, sampled, registry, desired_all, last_state, visible, row_fn):
    """Build visible usage rows, sorted by VM name."""
    rows = []
    for vm in sorted(set(per_vm) | set(sampled)):
        ev = per_vm.get(vm, {})
        sm = sampled.get(vm, {})
        owner = ev.get('owner') or sm.get('owner') or ''
        if not visible(owner):
            continue
        rows.append(row_fn(vm, ev, sm, registry, desired_all, last_state))
    return rows
