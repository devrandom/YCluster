"""CSV rendering for the admin inventory export."""

import csv
import io

FIELDS = [
    'hostname', 'type', 'ip',
    'product', 'serial', 'bios_version',
    'cpu', 'ram_gb', 'disks', 'gpus', 'nics',
    'os', 'kernel',
    'vendor', 'purchased_at', 'warranty_expires', 'cost', 'cost_currency',
    'location', 'notes', 'hw_collected_at', 'asset_updated_at',
]


def export_csv(rows):
    """Render inventory rows as CSV bytes in a StringIO."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FIELDS, extrasaction='ignore')
    writer.writeheader()
    for r in sorted(rows, key=lambda r: (r['allocation'] or {}).get('hostname', '') or ''):
        writer.writerow(_csv_row(r))
    return buf


def _csv_row(r):
    alloc = r['allocation'] or {}
    hw = r['hardware'] or {}
    asset = r['asset'] or {}
    return {
        'hostname': alloc.get('hostname', ''),
        'type': alloc.get('type', ''),
        'ip': alloc.get('ip', ''),
        'product': hw.get('product', ''),
        'serial': hw.get('serial', ''),
        'bios_version': hw.get('bios_version', ''),
        'cpu': hw.get('cpu', ''),
        'ram_gb': hw.get('ram_gb', ''),
        'disks': _join(f"{d['name']} {d['size']} {d['type']}" for d in hw.get('disks') or []),
        'gpus': _join(g.get('model') or g.get('vendor', '?') for g in hw.get('gpus') or []),
        'nics': _join(n['name'] + (f" {n['speed']}" if n.get('speed') else '') for n in hw.get('nics') or []),
        'os': hw.get('os', ''),
        'kernel': hw.get('kernel', ''),
        'vendor': asset.get('vendor', ''),
        'purchased_at': asset.get('purchased_at', ''),
        'warranty_expires': asset.get('warranty_expires', ''),
        'cost': asset.get('cost', ''),
        'cost_currency': asset.get('cost_currency', 'EUR'),
        'location': asset.get('location', ''),
        'notes': asset.get('notes', ''),
        'hw_collected_at': (hw.get('collected_at') or '')[:19],
        'asset_updated_at': (asset.get('updated_at') or '')[:19],
    }


def _join(values):
    return '; '.join(values)
