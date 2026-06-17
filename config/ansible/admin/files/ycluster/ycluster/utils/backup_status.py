"""Pure helpers + CLI for backup freshness / verify-restore Prometheus metrics.

The actual restores (psql, qdrant, etcdctl, age) live in the `backup-databases`
shell script; this module only turns "newest backup mtime" and "did the verify
load cleanly" into node-exporter textfile metrics. Kept dependency-free and
side-effect-free (apart from the thin `write` CLI) so the rendering logic is
unit-testable without a cluster.

Emitted to the node-exporter textfile dir; alerts live in
`monitoring/templates/ycluster-alerts.yml.j2`.
"""

import argparse
import os
import sys
import time

COMPONENTS = ("postgres", "qdrant", "etcd")


def newest_age_seconds(mtimes, now):
    """Age (seconds) of the newest mtime in `mtimes` relative to `now`, clamped
    to >= 0. Returns None for an empty iterable."""
    mtimes = list(mtimes)
    if not mtimes:
        return None
    return max(0.0, now - max(mtimes))


def scan_ages(encrypted_dir, now, components=COMPONENTS):
    """Map each component to the age (seconds) of its newest `*.age` backup
    under <encrypted_dir>/<component>/, or None if the dir is missing/empty."""
    ages = {}
    for component in components:
        comp_dir = os.path.join(encrypted_dir, component)
        mtimes = []
        try:
            for entry in os.scandir(comp_dir):
                if entry.name.endswith(".age") and entry.is_file():
                    mtimes.append(entry.stat().st_mtime)
        except FileNotFoundError:
            pass
        ages[component] = newest_age_seconds(mtimes, now)
    return ages


def render_metrics(now, ages, results, components=COMPONENTS):
    """Render the node-exporter textfile metrics.

    ages:    {component: age_seconds or None}
    results: {component: bool or None} — None means "not checked this run", so
             no success sample is emitted (the alert then fires only on a real 0,
             never on a component we deliberately skipped).
    """
    lines = [
        "# HELP ycluster_backup_restore_success Last verify-restore loaded cleanly (1=ok, 0=failed)",
        "# TYPE ycluster_backup_restore_success gauge",
    ]
    for component in components:
        result = results.get(component)
        if result is not None:
            lines.append(
                'ycluster_backup_restore_success{component="%s"} %d'
                % (component, 1 if result else 0)
            )
    lines += [
        "# HELP ycluster_backup_restore_timestamp_seconds Unix time of the last verify-restore run",
        "# TYPE ycluster_backup_restore_timestamp_seconds gauge",
        "ycluster_backup_restore_timestamp_seconds %d" % int(now),
        "# HELP ycluster_backup_age_seconds Age of the newest encrypted backup per component",
        "# TYPE ycluster_backup_age_seconds gauge",
    ]
    for component in components:
        age = ages.get(component)
        if age is not None:
            lines.append(
                'ycluster_backup_age_seconds{component="%s"} %d' % (component, int(age))
            )
    return "\n".join(lines) + "\n"


def parse_results(items):
    """Parse `component=0|1|skip` CLI pairs into {component: bool|None}. `skip`
    (the backup was absent, not checked) maps to None so no success sample is
    emitted. Unknown values raise ValueError so a typo fails loud rather than
    silently skipping a check."""
    results = {}
    for item in items or []:
        component, _, value = item.partition("=")
        if value == "skip":
            results[component] = None
        elif value in ("0", "1"):
            results[component] = value == "1"
        else:
            raise ValueError("result must be <component>=0|1|skip, got %r" % item)
    return results


def write_atomic(path, text):
    """Write `text` to `path` atomically (tmp + os.replace) so node-exporter
    never reads a half-written textfile."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--encrypted-dir", required=True,
                        help="root of the encrypted backups (…/<component>/*.age)")
    parser.add_argument("--out", required=True, help="textfile metric path to write")
    parser.add_argument("--result", action="append", default=[], metavar="COMPONENT=0|1",
                        help="verify-restore outcome per component (repeatable)")
    args = parser.parse_args(argv)

    now = time.time()
    try:
        results = parse_results(args.result)
    except ValueError as e:
        print("backup_status: %s" % e, file=sys.stderr)
        return 2
    ages = scan_ages(args.encrypted_dir, now)
    write_atomic(args.out, render_metrics(now, ages, results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
