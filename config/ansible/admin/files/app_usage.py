"""Helpers for the per-VM GPU usage endpoint."""

import csv
import io
import re

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


_GPU_ALIASES = (
    # lspci's database on some hosts predates Blackwell, so the card shows
    # up as a bare "Device 2bb5" — normalize both forms.
    (re.compile(r'rtx pro 6000|device 2bb5'), 'RTX PRO 6000'),
    (re.compile(r'rtx a6000'), 'RTX A6000'),
)
_STRIX_HALO = re.compile(r'strix halo|device 1586')
_AI_MAX_CPU = re.compile(r'ai max\+?\s*(\d+)', re.IGNORECASE)
_BMC_GPU = re.compile(r'aspeed|matrox', re.IGNORECASE)


def gpu_type_label(gpu, cpu=''):
    """Short operator-facing label for one inventory GPU entry.

    Inventory models are lspci marketing strings (or bare ``Device <id>``
    when the host's PCI database is older than the card) and nvidia-smi
    names. Normalize the ones the cluster uses to the names people say
    out loud; anything unrecognised is shown verbatim.
    """
    vendor = str(gpu.get('vendor') or '').strip()
    model = str(gpu.get('model') or '').strip()
    text = f'{vendor} {model}'.strip().lower()
    if not text or _BMC_GPU.search(text):
        return ''  # BMC display chips are not compute GPUs
    for pattern, label in _GPU_ALIASES:
        if pattern.search(text):
            return label
    if _STRIX_HALO.search(text):
        # Use the CPU family so a future Strix Halo part is not mislabelled
        # as the current Ryzen AI Max+ generation.
        match = _AI_MAX_CPU.search(cpu or '')
        return f'AMD {match.group(1)}+' if match else 'AMD Strix Halo'
    return model or vendor


def _hardware_gpu_type(hardware):
    """Distinct GPU type labels for a host's hardware snapshot."""
    labels = []
    for gpu in hardware.get('gpus') or []:
        label = gpu_type_label(gpu, hardware.get('cpu') or '')
        if label and label not in labels:
            labels.append(label)
    return ' + '.join(labels)


def gpu_types_by_host(registry, hardware_get=None):
    """Map each VM host in the registry to its GPU type label.

    Best-effort: inventory gaps or an etcd failure leave the label empty
    rather than failing the usage page.
    """
    if hardware_get is None:
        from ycluster.utils.inventory import get_hardware as hardware_get
    hosts = {(rec or {}).get('host') for rec in registry.values()}
    out = {}
    for host in sorted(h for h in hosts if h):
        try:
            hardware = hardware_get(host) or {}
            out[host] = _hardware_gpu_type(hardware)
        except Exception:
            out[host] = ''
    return out


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


def usage_rows(per_vm, sampled, registry, desired_all, last_state, visible,
               row_fn, gpu_types=None):
    """Build visible usage rows, sorted by VM name."""
    rows = []
    for vm in sorted(set(per_vm) | set(sampled)):
        ev = per_vm.get(vm, {})
        sm = sampled.get(vm, {})
        owner = ev.get('owner') or sm.get('owner') or ''
        if not visible(owner):
            continue
        row = row_fn(vm, ev, sm, registry, desired_all, last_state)
        if gpu_types is not None:
            host = (registry.get(vm) or {}).get('host', '')
            row['gpu_type'] = gpu_types.get(host, '') if row.get('gpus') else ''
        rows.append(row)
    return rows


CSV_FIELDS = ['Owner', 'VM', 'GPUs', 'GPU type', 'Now', 'Schedule',
              'Windows', 'Billable GPU-h', 'Tracked GPU-h',
              'Observed GPU-h', 'Delta GPU-h', 'Samples']


def usage_csv(rows):
    """Render usage rows as a CSV StringIO, matching the page's columns."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
    writer.writeheader()
    for row in rows:
        writer.writerow({
            'Owner': row.get('owner') or '(unregistered)',
            'VM': row['vm'],
            'GPUs': row.get('gpus') if row.get('gpus') is not None else '',
            'GPU type': row.get('gpu_type') or '',
            'Now': (f"running ({row['current']})"
                    if row.get('current') else ''),
            'Schedule': row.get('schedule_mode') or '',
            'Windows': '; '.join(f"{w.get('start', '')} -> {w.get('end', '')}"
                                 for w in row.get('windows') or []),
            'Billable GPU-h': f"{row.get('billable_gpu_hours', 0.0):.2f}",
            'Tracked GPU-h': f"{row.get('tracked_gpu_hours', 0.0):.2f}",
            'Observed GPU-h': f"{row.get('observed_gpu_hours', 0.0):.2f}",
            'Delta GPU-h': (
                f"{row.get('observed_gpu_hours', 0.0) - row.get('tracked_gpu_hours', 0.0):.2f}"),
            'Samples': row.get('samples', 0),
        })
    buf.seek(0)
    return buf
