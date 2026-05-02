"""
Hardware inventory and asset management commands.
"""

import csv
import io
import json
import sys

from ..utils import inventory as inv


def register_inventory_commands(subparsers):
    p = subparsers.add_parser('inventory', help='Hardware inventory and asset management')
    p.set_defaults(func=lambda args: args.parser.print_help(), parser=p)
    sub = p.add_subparsers(dest='inventory_command', help='inventory commands')

    # show
    show_p = sub.add_parser('show', help='Show inventory for one or all nodes')
    show_p.add_argument('hostname', nargs='?', help='Hostname to show (omit for all)')
    show_p.add_argument('--json', dest='as_json', action='store_true', help='JSON output')
    show_p.set_defaults(func=_show)

    # collect
    collect_p = sub.add_parser('collect', help='Collect hardware facts for this node and persist to etcd')
    collect_p.add_argument('--from-file', dest='from_file', metavar='FILE',
                           help='Load pre-collected facts from JSON file instead of collecting locally')
    collect_p.add_argument('--hostname', dest='hostname', metavar='HOSTNAME',
                           help='Target hostname when using --from-file (defaults to local hostname)')
    collect_p.set_defaults(func=_collect)

    # set-asset
    asset_p = sub.add_parser('set-asset', help='Set asset metadata fields for a node')
    asset_p.add_argument('hostname', help='Target hostname')
    asset_p.add_argument('--vendor', help='Hardware vendor / manufacturer')
    asset_p.add_argument('--purchased-at', dest='purchased_at', metavar='YYYY-MM-DD')
    asset_p.add_argument('--warranty-expires', dest='warranty_expires', metavar='YYYY-MM-DD')
    asset_p.add_argument('--cost', dest='cost', type=float, metavar='N')
    asset_p.add_argument('--cost-currency', dest='cost_currency', default=None, metavar='EUR|USD')
    asset_p.add_argument('--location', help='Physical location / rack slot')
    asset_p.add_argument('--notes', help='Free-text notes')
    asset_p.set_defaults(func=_set_asset)

    # export
    export_p = sub.add_parser('export', help='Export full inventory as CSV')
    export_p.add_argument('--output', '-o', metavar='FILE',
                          help='Write to file instead of stdout')
    export_p.set_defaults(func=_export)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _show(args):
    if args.hostname:
        hw = inv.get_hardware(args.hostname)
        asset = inv.get_asset(args.hostname)
        data = {'hostname': args.hostname, 'hardware': hw, 'asset': asset}
        if args.as_json:
            print(json.dumps(data, indent=2))
        else:
            _print_node(args.hostname, hw, asset)
    else:
        rows = inv.list_all()
        rows.sort(key=lambda r: (r['allocation'] or {}).get('hostname', '') or
                                 (r['hardware'] or {}).get('hostname', '') or '')
        if args.as_json:
            out = []
            for r in rows:
                alloc = r['allocation'] or {}
                out.append({
                    'hostname': alloc.get('hostname'),
                    'type': alloc.get('type'),
                    'ip': alloc.get('ip'),
                    'hardware': r['hardware'],
                    'asset': r['asset'],
                })
            print(json.dumps(out, indent=2))
        else:
            for r in rows:
                alloc = r['allocation'] or {}
                _print_node(alloc.get('hostname', '?'), r['hardware'], r['asset'])
                print()


def _print_node(hostname, hw, asset):
    print(f"=== {hostname} ===")
    if hw:
        print(f"  Product:   {hw.get('product') or '-'}")
        print(f"  Serial:    {hw.get('serial') or '-'}")
        print(f"  CPU:       {hw.get('cpu') or '-'}")
        ram = f"{hw['ram_gb']} GB" if hw.get('ram_gb') else '-'
        print(f"  RAM:       {ram}")
        disks = hw.get('disks') or []
        if disks:
            disk_str = ', '.join(f"{d['name']} {d['size']} {d['type']}" +
                                  (f" ({d['model']})" if d.get('model') else '')
                                  for d in disks)
            print(f"  Disks:     {disk_str}")
        gpus = hw.get('gpus') or []
        if gpus:
            gpu_str = ', '.join(g.get('model') or g.get('vendor', '?') for g in gpus)
            print(f"  GPUs:      {gpu_str}")
        nics = hw.get('nics') or []
        if nics:
            nic_str = ', '.join(f"{n['name']}" + (f" {n['speed']}" if n.get('speed') else '') for n in nics)
            print(f"  NICs:      {nic_str}")
        print(f"  OS:        {hw.get('os') or '-'}  kernel {hw.get('kernel') or '-'}")
        print(f"  BIOS:      {hw.get('bios_version') or '-'}")
        print(f"  Collected: {hw.get('collected_at', '-')[:19]}")
    else:
        print("  [no hardware data — run: ycluster inventory collect]")

    if asset:
        print(f"  Vendor:    {asset.get('vendor') or '-'}")
        print(f"  Purchased: {asset.get('purchased_at') or '-'}")
        print(f"  Warranty:  {asset.get('warranty_expires') or '-'}")
        cost = asset.get('cost')
        currency = asset.get('cost_currency', 'EUR')
        symbols = {'EUR': '€', 'USD': '$'}
        sym = symbols.get(currency, currency)
        print(f"  Cost:      {f'{sym}{cost:.2f}' if cost else '-'}")
        print(f"  Location:  {asset.get('location') or '-'}")
        if asset.get('notes'):
            print(f"  Notes:     {asset['notes']}")


def _collect(args):
    import json as _json
    import platform as _platform
    from_file = getattr(args, 'from_file', None)
    hostname = getattr(args, 'hostname', None) or _platform.node()
    if from_file:
        with open(from_file) as f:
            facts = _json.load(f)
        print(f"Loaded facts from {from_file} for {hostname}")
    else:
        print(f"Collecting hardware facts for {hostname}...")
        facts = inv.collect_hardware()
    inv.put_hardware(hostname, facts)
    print(f"Stored at /cluster/nodes/hardware/{hostname}")
    _print_node(hostname, facts, inv.get_asset(hostname))


def _set_asset(args):
    fields = {}
    for key in ('vendor', 'purchased_at', 'warranty_expires', 'cost', 'cost_currency', 'location', 'notes'):
        val = getattr(args, key, None)
        if val is not None:
            fields[key] = val
    if not fields:
        print("No fields specified.", file=sys.stderr)
        sys.exit(1)
    result = inv.put_asset(args.hostname, fields)
    print(json.dumps(result, indent=2))


def _export(args):
    rows = inv.list_all()
    rows.sort(key=lambda r: (r['allocation'] or {}).get('hostname', '') or '')

    fieldnames = [
        'hostname', 'type', 'ip',
        'product', 'serial', 'bios_version',
        'cpu', 'ram_gb',
        'disks', 'gpus', 'nics',
        'os', 'kernel',
        'vendor', 'purchased_at', 'warranty_expires', 'cost', 'cost_currency', 'location', 'notes',
        'hw_collected_at', 'asset_updated_at',
    ]

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction='ignore')
    writer.writeheader()

    for r in rows:
        alloc = r['allocation'] or {}
        hw = r['hardware'] or {}
        asset = r['asset'] or {}

        disks = '; '.join(
            f"{d['name']} {d['size']} {d['type']}" for d in (hw.get('disks') or [])
        )
        gpus = '; '.join(
            g.get('model') or g.get('vendor', '?') for g in (hw.get('gpus') or [])
        )
        nics = '; '.join(
            f"{n['name']}" + (f" {n['speed']}" if n.get('speed') else '')
            for n in (hw.get('nics') or [])
        )

        writer.writerow({
            'hostname':        alloc.get('hostname', ''),
            'type':            alloc.get('type', ''),
            'ip':              alloc.get('ip', ''),
            'product':         hw.get('product', ''),
            'serial':          hw.get('serial', ''),
            'bios_version':    hw.get('bios_version', ''),
            'cpu':             hw.get('cpu', ''),
            'ram_gb':          hw.get('ram_gb', ''),
            'disks':           disks,
            'gpus':            gpus,
            'nics':            nics,
            'os':              hw.get('os', ''),
            'kernel':          hw.get('kernel', ''),
            'vendor':          asset.get('vendor', ''),
            'purchased_at':    asset.get('purchased_at', ''),
            'warranty_expires': asset.get('warranty_expires', ''),
            'cost':            asset.get('cost', ''),
            'cost_currency':   asset.get('cost_currency', 'EUR'),
            'location':        asset.get('location', ''),
            'notes':           asset.get('notes', ''),
            'hw_collected_at': (hw.get('collected_at') or '')[:19],
            'asset_updated_at': (asset.get('updated_at') or '')[:19],
        })

    output = buf.getvalue()
    if args.output:
        with open(args.output, 'w') as f:
            f.write(output)
        print(f"Wrote {args.output}")
    else:
        print(output, end='')
