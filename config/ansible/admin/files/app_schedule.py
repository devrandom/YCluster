"""Helpers for the VM schedule data endpoint."""

from datetime import datetime


def live_state(states):
    """Return ({vm: status}, {host: pool_size}) from host snapshots."""
    live, pools = {}, {}
    for host, snap in states.items():
        if isinstance(snap.get('gpu_pool'), int):
            pools[host] = snap['gpu_pool']
        for inst in snap.get('instances', []):
            live[inst['name']] = inst.get('status')
    return live, pools


def owner_filters(identity, authentik_manager):
    """Return (visible, shown) predicates for owner scoping/redaction."""
    managed = None
    cache = {}
    if identity.is_manager and not identity.is_admin:
        values = authentik_manager.manager_projects(identity.email) \
            if identity.email else []
        managed = set(values)

        def visible(owner):
            if owner not in cache:
                try:
                    project = authentik_manager.user_project(owner) if owner else ''
                except Exception:
                    # Orphan owner or transient IdP failure: fail closed.
                    project = ''
                cache[owner] = project
            return cache[owner] in managed
    else:
        def visible(owner):
            return True

    def shown(owner):
        return identity.is_admin or (managed is not None and visible(owner))

    return visible, shown


def schedule_rows(id, registry, desired_all, issues, graces, live, commitments,
                  pools, now, can_edit_vm, parse_windows, vmm,
                  authentik_manager):
    """Build schedule rows for every VM visible to this caller."""
    visible, _ = owner_filters(id, authentik_manager)
    rows = []
    for name, rec in registry.items():
        if not id.is_manager and rec.get('owner') != id.email:
            continue
        if not visible(rec.get('owner')):
            continue
        desired = desired_all.get(name)
        row = schedule_row(id, name, rec, desired, issues, graces, live, now,
                           can_edit_vm, vmm)
        add_suggestions(row, rec, desired, commitments, pools, parse_windows,
                        now, vmm)
        rows.append(row)
    return rows


def schedule_row(id, name, rec, desired, issues, graces, live, now,
                 can_edit_vm, vmm):
    """Render one VM schedule row."""
    mode = (desired or {}).get('mode', 'unmanaged')
    status = live.get(name, 'unknown')
    return {
        'vm': name,
        'owner': rec.get('owner'),
        'host': rec.get('host'),
        'gpus': rec.get('gpus'),
        # Desired GPU count (defaults to registered); the page's GPU
        # selector starts here.
        'sched_gpus': vmm.desired_gpus(desired, rec.get('gpus') or 0),
        # Public frp port for the host's jump bastion.
        'port': vmm.bastion_public_port(rec.get('host')),
        'status': status,
        'mode': mode,
        'windows': (desired or {}).get('windows', []),
        'issue': issues.get(name),
        'pending': vmm.pending_state(desired, status, graces.get(name), now),
        'grace': graces.get(name),
        # Explicit admin-set unmanaged records are locked for non-admins.
        'has_desired': desired is not None,
        'can_edit': can_edit_vm(id, rec, desired),
        'admin_locked': desired is not None and mode == 'unmanaged',
    }


def add_suggestions(row, rec, desired, commitments, pools, parse_windows, now,
                    vmm):
    """Attach acquire suggestions where a passthrough pool is present."""
    pool = pools.get(rec.get('host'))
    if rec.get('type') == 'container' or not isinstance(pool, int):
        row['suggestions'] = []
        return
    host_commits = [c for c in commitments.get(rec.get('host'), [])
                    if c['vm'] != row['vm']]
    own_windows = [
        (datetime.fromisoformat(w['start']), datetime.fromisoformat(w['end']))
        for w in parse_windows((desired or {}).get('windows', [])) or []
    ]
    row['suggestions'] = vmm.gpu_available_windows(
        host_commits, row['sched_gpus'], pool, now, avoid_windows=own_windows)


def schedule_hosts(id, rows, blocks, commitments, pools, visible_owner):
    """Build host capacity entries visible to this caller."""
    visible_hosts = {r['host'] for r in rows if r['host']}
    block_hosts = {b.get('host') for b in blocks.values() if b.get('host')}
    hosts = []
    for host in sorted(set(commitments) | set(pools)):
        if not pools.get(host) and not commitments.get(host):
            continue  # container hosts: no passthrough pool
        if not id.is_admin and host not in visible_hosts:
            # Managers keep GPU-block hosts; plain users only their VM hosts.
            if not id.is_manager or host not in block_hosts:
                continue
        hosts.append({
            'host': host,
            'pool': pools.get(host),
            'commitments': render_commitments(commitments.get(host, []),
                                              visible_owner),
        })
    return hosts


def render_commitments(commits, visible_owner):
    """Render commitments, redacting owners unless visible to the caller."""
    return [{
        'vm': c['vm'], 'gpus': c['gpus'], 'mode': c['mode'],
        'is_block': c.get('is_block', False),
        'owner': c['owner'] if visible_owner(c['owner']) else None,
        'windows': None if c['windows'] is None else [
            {'start': s.isoformat(), 'end': e.isoformat()}
            for s, e in c['windows']],
    } for c in commits]
