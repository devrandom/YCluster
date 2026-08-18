"""Command line interface for osv-feed."""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys

from . import __version__
from .acks import add_ack
from .acks import find_ack
from .acks import list_acks
from .acks import load_acks
from .acks import remove_ack
from .categories import DEFAULT_FALLBACK_CATEGORY
from .categories import DEFAULT_FALLBACK_NAME
from .categories import category_ecosystem
from .categories import category_packages
from .categories import matched_interests
from .categories import passes_category
from .config import categories_for_package
from .config import category_config
from .config import default_db_dir
from .config import load_config
from .config import resolve_ack_db
from .config import resolve_container_dirs
from .config import resolve_db_dir
from .config import resolve_immutable_db
from .config import resolve_state_dir
from .config import tracking_categories
from .installed import PackageNotInstalledError
from .installed import host_ecosystem
from .installed import installed_package
from .matching import UNINSTALLED
from .matching import first_seen_for
from .matching import match_container
from .matching import match_specs
from .epss import mirror_epss
from .mirror import feed_status
from .mirror import mirror_ecosystem


def _escape_prometheus(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


def _acked_label(finding: dict) -> str:
    return "true" if finding.get("acked") else "false"


def _image_label(finding: dict) -> str:
    """Prometheus ``,image="…"`` label segment for a container finding."""
    image = finding.get("image")
    if not image:
        return ""
    return f',image="{_escape_prometheus(image)}"'


def _render_prometheus(findings: list[dict], first_seen: dict[str, int]) -> str:
    lines = [
        "# HELP osv_feed_cve_info CVE state for an installed package version.",
        "# TYPE osv_feed_cve_info gauge",
    ]
    for finding in findings:
        if finding.get("state") == UNINSTALLED:
            continue
        labels = (
            f'ecosystem="{_escape_prometheus(finding["ecosystem"])}",'
            f'package="{_escape_prometheus(finding["requested_package"])}",'
            f'version="{_escape_prometheus(finding["version"])}",'
            f'cve="{_escape_prometheus(finding["cve"])}",'
            f'severity="{_escape_prometheus(finding["severity"])}",'
            f'state="{_escape_prometheus(finding["state"])}",'
            f'category="{_escape_prometheus(finding.get("category", ""))}",'
            f'acked="{_acked_label(finding)}"'
            f'{_image_label(finding)}'
        )
        lines.append(f"osv_feed_cve_info{{{labels}}} 1")
    lines.append("# HELP osv_feed_cve_epss_score EPSS exploit-probability score for a CVE (0 if unknown).")
    lines.append("# TYPE osv_feed_cve_epss_score gauge")
    for finding in findings:
        if finding.get("state") == UNINSTALLED:
            continue
        labels = (
            f'ecosystem="{_escape_prometheus(finding["ecosystem"])}",'
            f'package="{_escape_prometheus(finding["requested_package"])}",'
            f'version="{_escape_prometheus(finding["version"])}",'
            f'cve="{_escape_prometheus(finding["cve"])}"'
            f'{_image_label(finding)}'
        )
        lines.append(f"osv_feed_cve_epss_score{{{labels}}} {finding.get('epss_score', 0.0)}")
    lines.append("# HELP osv_feed_cve_epss_percentile EPSS percentile (0..1) for a CVE (0 if unknown).")
    lines.append("# TYPE osv_feed_cve_epss_percentile gauge")
    for finding in findings:
        if finding.get("state") == UNINSTALLED:
            continue
        labels = (
            f'ecosystem="{_escape_prometheus(finding["ecosystem"])}",'
            f'package="{_escape_prometheus(finding["requested_package"])}",'
            f'version="{_escape_prometheus(finding["version"])}",'
            f'cve="{_escape_prometheus(finding["cve"])}"'
            f'{_image_label(finding)}'
        )
        lines.append(
            f"osv_feed_cve_epss_percentile{{{labels}}} {finding.get('epss_percentile', 0.0)}"
        )
    lines.append("# HELP osv_feed_cve_first_seen_seconds Seconds since epoch when a CVE was first seen for a package version.")
    lines.append("# TYPE osv_feed_cve_first_seen_seconds gauge")
    for key, value in first_seen.items():
        parts = key.split("|")
        if parts[0] == "image":
            _, image, ecosystem, package, version, cve = parts
            image_label = f',image="{_escape_prometheus(image)}"'
        else:
            ecosystem, package, version, cve = parts
            image_label = ""
        labels = (
            f'ecosystem="{_escape_prometheus(ecosystem)}",'
            f'package="{_escape_prometheus(package)}",'
            f'version="{_escape_prometheus(version)}",'
            f'cve="{_escape_prometheus(cve)}"'
            f'{image_label}'
        )
        lines.append(f"osv_feed_cve_first_seen_seconds{{{labels}}} {value}")
    lines.append("")
    return "\n".join(lines)


def _cve_title(finding: dict) -> str:
    """Short human title for a finding.

    Prefers the OSV ``summary`` when present; distro feeds (notably Debian)
    often leave ``summary`` empty and describe the issue only in ``details``,
    so fall back to the first sentence of ``details`` to keep the verbose
    output useful.
    """
    title = (finding.get("summary") or "").strip()
    if title:
        return title
    details = (finding.get("details") or "").strip()
    if not details:
        return ""
    match = re.match(r"(.+?)(?:[.!?](?:\s|$)|(?:\n|$))", details, re.DOTALL)
    return (match.group(1) if match else details).strip()


def _verbose_detail_lines(finding: dict) -> list[str]:
    lines: list[str] = []
    title = _cve_title(finding)
    if title:
        lines.append(f"    title:       {title}")
    interests = finding.get("interests") or []
    if interests:
        lines.append(f"    interests:   {', '.join(interests)}")
    aliases = finding.get("aliases") or []
    if aliases:
        lines.append(f"    aliases:     {', '.join(aliases)}")
    references = finding.get("references") or []
    if references:
        lines.append(f"    references:  {', '.join(references)}")
    return lines


def _format_acked_at(value: str) -> str:
    """Short human ``acked_at`` for table columns (keeps full ISO in json)."""
    if not value:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _render_table(findings: list[dict], verbose: bool = False) -> str:
    header = ("ECOSYSTEM", "CATEGORY", "PACKAGE", "VERSION", "SEVERITY", "CVE", "STATE", "ACK")
    rows = [
        (
            f["ecosystem"],
            f.get("category", ""),
            f["requested_package"],
            f["version"],
            f["severity"],
            f["cve"],
            f["state"],
            _format_acked_at(f.get("acked_at", "")),
        )
        for f in findings
    ]
    widths = [max(len(str(row[i])) for row in [header] + rows) for i in range(len(header))]
    lines = ["  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(header))]
    lines.append("  ".join("-" * width for width in widths))
    for row, finding in zip(rows, findings):
        lines.append("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))
        if verbose:
            lines.extend(_verbose_detail_lines(finding))
    return "\n".join(lines)


def _render_ack_table(entries: list[dict]) -> str:
    header = ("CATEGORY", "ECOSYSTEM", "CVE", "SURFACE", "ACKED_AT", "NOTE")
    rows = [
        (
            entry.get("category", ""),
            entry.get("ecosystem", ""),
            entry.get("cve", ""),
            entry.get("surface", ""),
            _format_acked_at(entry.get("acked_at", "")),
            entry.get("note", ""),
        )
        for entry in entries
    ]
    widths = [max(len(str(row[i])) for row in [header] + rows) for i in range(len(header))]
    lines = ["  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(header))]
    lines.append("  ".join("-" * width for width in widths))
    for row in rows:
        lines.append("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))
    return "\n".join(lines)


def _mirror_ecosystems(args: argparse.Namespace) -> list[str]:
    if args.ecosystem:
        return args.ecosystem
    default = host_ecosystem()
    if default is None:
        raise SystemExit(
            "cannot determine default ecosystem: host is not Debian/Ubuntu; "
            "pass --ecosystem"
        )
    return [default]


def _warn_stale_feeds(db_dir: str, specs: list[dict]) -> bool:
    missing = False
    for ecosystem in sorted({spec["ecosystem"] for spec in specs}):
        status = feed_status(db_dir, ecosystem)
        if status == "absent":
            missing = True
            print(
                f"osv-feed: warning: {ecosystem} feed is absent; "
                f"run 'osv-feed mirror --ecosystem {ecosystem}' first",
                file=sys.stderr,
            )
        elif status == "stale":
            print(
                f"osv-feed: warning: {ecosystem} feed is more than a day old; "
                f"run 'osv-feed mirror --ecosystem {ecosystem}'",
                file=sys.stderr,
            )
    return missing


def _resolve_spec(
    name: str,
    version: str | None,
    resolver,
) -> tuple[str, str | None] | None:
    if version:
        return name, version
    try:
        resolved = resolver(name)
    except PackageNotInstalledError:
        # Not installed: report it as an uninstalled finding later rather than
        # dropping the package, but only when no version was given.
        return name, None
    except LookupError as exc:
        print(f"osv-feed: warning: {exc}", file=sys.stderr)
        return None
    return resolved.name, resolved.version


def _add_spec(specs: list[dict], ecosystem: str, name: str, version: str | None, resolver) -> None:
    resolved = _resolve_spec(name, version, resolver)
    if resolved is None:
        return
    package, resolved_version = resolved
    specs.append(
        {
            "ecosystem": ecosystem,
            "package": package,
            "version": resolved_version,
        }
    )


def _category_specs(
    category: str,
    category_cfg: dict,
    default_ecosystem: str | None,
    resolver,
) -> list[dict]:
    specs: list[dict] = []
    ecosystem = category_ecosystem(category_cfg, default_ecosystem)
    if ecosystem is None:
        raise SystemExit(
            f"category {category!r} has no ecosystem and the host OS is not "
            "Debian/Ubuntu; set [tracking.<name>].ecosystem or pass --ecosystem"
        )
    for package in category_packages(category_cfg):
        if "=" in package:
            name, version = package.split("=", 1)
        else:
            name, version = package, None
        _add_spec(specs, ecosystem, name, version, resolver)
    return specs


def _all_category_specs(
    config: dict,
    default_ecosystem: str | None,
    resolver,
) -> list[dict]:
    """Match specs for every package listed in any tracking category.

    Packages are deduplicated by (ecosystem, package, version); if a package
    is listed in several categories, it is still matched once and re-tagged
    per category later by ``_filtered_findings``.
    """
    specs: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for table in tracking_categories(config).values():
        ecosystem = category_ecosystem(table, default_ecosystem)
        if ecosystem is None:
            raise SystemExit(
                "a tracking category has no ecosystem and the host OS is not "
                "Debian/Ubuntu; set its ecosystem or pass --ecosystem"
            )
        for entry in category_packages(table):
            name, sep, version = entry.partition("=")
            resolved = _resolve_spec(name, version or None, resolver)
            if resolved is None:
                continue
            package, resolved_version = resolved
            key = (ecosystem, package, resolved_version)
            if key in seen:
                continue
            seen.add(key)
            specs.append(
                {
                    "ecosystem": ecosystem,
                    "package": package,
                    "version": resolved_version,
                }
            )
    return specs


def _parse_specs(
    args: argparse.Namespace,
    config: dict | None = None,
    resolver=None,
) -> list[dict]:
    if resolver is None:
        resolver = installed_package
    default_ecosystem = args.ecosystem or host_ecosystem()
    specs: list[dict] = []
    category = getattr(args, "category", None)
    requested = bool(category) or bool(args.pkg) or bool(args.spec_file)
    if category:
        category_cfg = category_config(category, config)
        if category_cfg is None:
            raise SystemExit(f"category {category!r} is unknown")
        specs.extend(_category_specs(category, category_cfg, default_ecosystem, resolver))
    for spec in args.pkg or []:
        if default_ecosystem is None:
            raise SystemExit(
                "cannot determine ecosystem for --pkg: host is not "
                "Debian/Ubuntu and no --ecosystem was given"
            )
        if "=" in spec:
            name, version = spec.split("=", 1)
        else:
            name, version = spec, None
        _add_spec(specs, default_ecosystem, name, version, resolver)
    if args.spec_file:
        try:
            with open(args.spec_file, "r", encoding="utf-8") as fh:
                content = fh.read()
            data = json.loads(content)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"cannot read --spec-file {args.spec_file}: {exc}") from exc
        if isinstance(data, dict):
            data = [data]
        for item in data:
            missing = {"ecosystem", "package"} - set(item)
            if missing:
                raise SystemExit(f"spec missing keys: {sorted(missing)}")
            _add_spec(specs, item["ecosystem"], item["package"], item.get("version"), resolver)
    if not specs and not requested:
        specs = _all_category_specs(config or {}, default_ecosystem, resolver)
    return specs


def _tag_finding(finding: dict, category: str, category_cfg: dict | None, acks: dict) -> dict | None:
    entry = dict(finding)
    entry["category"] = category
    if (
        entry.get("state") != UNINSTALLED
        and category_cfg is not None
        and not passes_category(entry, category_cfg)
    ):
        return None
    entry["interests"] = (
        matched_interests(entry, category_cfg) if category_cfg is not None else []
    )
    surface = entry.get("surface") or entry.get("image") or ""
    ack = find_ack(acks, category, entry["ecosystem"], entry["cve"], surface)
    entry["acked"] = ack is not None
    if ack:
        entry["acked_at"] = ack.get("acked_at", "")
        entry["ack_note"] = ack.get("note", "")
    if surface:
        entry["surface"] = surface
    return entry


def _auto_categories(package: str, config: dict) -> list[tuple[str, dict | None]]:
    """Categories for a package when no ``--category`` was given.

    Returns every category whose package set includes ``package``, falling
    back to a single medium-policy ``default`` category when it belongs to
    none.
    """
    names = categories_for_package(package, config)
    if names:
        return [(name, category_config(name, config)) for name in names]
    return [(DEFAULT_FALLBACK_NAME, DEFAULT_FALLBACK_CATEGORY)]


def _filtered_findings(findings: list[dict], args: argparse.Namespace, config: dict, acks: dict) -> list[dict]:
    filtered: list[dict] = []
    category = args.category or ""
    if category:
        category_cfg = category_config(category, config)
        for finding in findings:
            tagged = _tag_finding(finding, category, category_cfg, acks)
            if tagged is not None:
                filtered.append(tagged)
        return filtered
    for finding in findings:
        for cat_name, cat_cfg in _auto_categories(finding["package"], config):
            tagged = _tag_finding(finding, cat_name, cat_cfg, acks)
            if tagged is not None:
                filtered.append(tagged)
    return filtered


def _filter_container_findings(
    findings: list[dict], config: dict, acks: dict
) -> list[dict]:
    """Tag container findings ``category=container`` and apply its policy.

    Container findings are driven by manifest files rather than a category
    ``packages`` list, so they are tagged directly and filtered through the
    ``[tracking.container]`` table (built-in default: high severity, pending
    states only).
    """
    category_cfg = category_config("container", config)
    filtered: list[dict] = []
    for finding in findings:
        tagged = _tag_finding(finding, "container", category_cfg, acks)
        if tagged is not None:
            filtered.append(tagged)
    return filtered


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="osv-feed",
        description="Leak-free local OSV vulnerability feed mirror and matcher",
    )
    parser.add_argument("--version", action="version", version=f"osv-feed {__version__}")
    parser.add_argument(
        "--db-dir",
        default=None,
        help=(
            "local mirror directory (default ~/.cache/osv-feed; env "
            "OSV_FEED_DB_DIR overrides config, CLI wins)"
        ),
    )
    parser.add_argument(
        "--ack-db",
        default=None,
        help=(
            "acknowledgment DB path (default ~/.cache/osv-feed-acks.json; "
            "env OSV_FEED_ACK_DB overrides config, CLI wins)"
        ),
    )
    parser.add_argument(
        "--db-immutable",
        action="store_true",
        default=None,
        help=(
            "open the mirrored store immutable/read-only (no locking, no "
            "journal writes) for consumers pointed at an atomic snapshot, "
            "e.g. on a network share; env OSV_FEED_IMMUTABLE_DB and "
            "[feed] immutable_db also work, CLI wins"
        ),
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help=(
            "writable directory for first-seen state, separate from a "
            "read-only snapshot DB dir when --db-immutable is used; env "
            "OSV_FEED_STATE_DIR overrides [feed] state_dir, CLI wins"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    mirror = subparsers.add_parser("mirror", help="sync OSV ecosystem data locally")
    mirror.add_argument(
        "--ecosystem",
        action="append",
        default=None,
        help=(
            "OSV ecosystem to mirror (repeatable; defaults to the host OS "
            "when it is Debian or Ubuntu)"
        ),
    )
    mirror.add_argument(
        "--force-full",
        action="store_true",
        help="re-download the full ecosystem archive even if a cursor exists",
    )
    mirror.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="suppress per-file download progress on stderr",
    )

    match = subparsers.add_parser("match", help="match local versions against the mirror")
    match.add_argument(
        "--ecosystem",
        default=None,
        help=(
            "default ecosystem for --pkg and category packages; defaults to "
            "the host OS when it is Debian or Ubuntu"
        ),
    )
    match.add_argument("--category", help="filter to a single tracking category (built-in or [tracking.<name>]); omit to auto-categorize the given packages")
    match.add_argument("--pkg", action="append", help="NAME[=VERSION] to match (repeatable; omitted version resolves the installed package and maps binary names to source)")
    match.add_argument("--spec-file", help="JSON object or array of {ecosystem, package, version} (omitted version resolves the installed package)")
    match.add_argument(
        "--container-dir",
        action="append",
        default=None,
        help=(
            "directory of container/*.manifest files (ecosystem:name=version "
            "per line) to match; findings are tagged category=container with an "
            "image label (repeatable)"
        ),
    )
    match.add_argument(
        "--format",
        choices=("table", "json", "prometheus"),
        default="table",
        help="output format (default table)",
    )
    match.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="table only: show the CVE title and important attributes (matched interests, aliases, reference URLs)",
    )

    ack = subparsers.add_parser("ack", help="manage CVEs acknowledged for tracking categories")
    ack_sub = ack.add_subparsers(dest="ack_command", required=True)

    ack_add = ack_sub.add_parser("add", help="acknowledge a CVE so alerts can suppress it")
    ack_add.add_argument("--category", required=True)
    ack_add.add_argument("--ecosystem")
    ack_add.add_argument(
        "--surface",
        default="",
        help=(
            "deployment surface to scope the ack to (e.g. a container image "
            "name, or a host install like local-ai-proxy); empty acknowledges "
            "the CVE on every surface"
        ),
    )
    ack_add.add_argument("--cve", required=True)
    ack_add.add_argument("--note", default="")

    ack_remove = ack_sub.add_parser("remove", help="remove a CVE acknowledgment")
    ack_remove.add_argument("--category", required=True)
    ack_remove.add_argument("--ecosystem")
    ack_remove.add_argument(
        "--surface",
        default="",
        help="surface the ack is scoped to (omit for a wildcard/surface-less ack)",
    )
    ack_remove.add_argument("--cve", required=True)

    ack_list = ack_sub.add_parser("list", help="list acknowledged CVEs")
    ack_list.add_argument("--category")
    ack_list.add_argument(
        "--surface",
        default=None,
        help="only show acks scoped to this surface (wildcard surface-less acks are shown with an empty column)",
    )
    ack_list.add_argument("--format", choices=("table", "json"), default="table")
    return parser


def _run_ack(args: argparse.Namespace, config: dict) -> int:
    if args.ack_command == "list":
        entries = list_acks(args.ack_db, args.category, surface=args.surface)
        if args.format == "json":
            json.dump(entries, sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            sys.stdout.write(_render_ack_table(entries) + "\n")
        return 0
    ecosystem = args.ecosystem or ""
    surface = args.surface or ""
    ecosystem_label = ecosystem or "any ecosystem"
    surface_label = f", surface {surface!r}" if surface else ""
    if args.ack_command == "add":
        entry = add_ack(
            args.ack_db,
            category=args.category,
            ecosystem=ecosystem,
            cve=args.cve,
            surface=surface,
            note=args.note,
        )
        print(
            f"acknowledged {entry['cve']} in {entry['category']} "
            f"({ecosystem_label}{surface_label})"
        )
        return 0
    if remove_ack(
        args.ack_db,
        category=args.category,
        ecosystem=ecosystem,
        cve=args.cve,
        surface=surface,
    ):
        print(
            f"removed ack for {args.cve} in {args.category} "
            f"({ecosystem_label}{surface_label})"
        )
        return 0
    print(
        f"no ack found for {args.cve} in {args.category} "
        f"({ecosystem_label}{surface_label})",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config()
    args.db_dir = resolve_db_dir(args.db_dir)
    args.ack_db = resolve_ack_db(args.ack_db)
    args.immutable = resolve_immutable_db(args.db_immutable)
    # First-seen state is writable and must never land on a read-only
    # snapshot share; fall back to the local default DB dir when immutable.
    args.state_dir = resolve_state_dir(
        args.state_dir,
        default_db_dir() if args.immutable else args.db_dir,
    )
    if args.command == "mirror":
        progress = None if args.quiet else (lambda message: print(message, file=sys.stderr))
        for ecosystem in _mirror_ecosystems(args):
            result = mirror_ecosystem(
                args.db_dir, ecosystem, force_full=args.force_full, progress=progress
            )
            print(f"{ecosystem}: downloaded={result['downloaded']} removed={result['removed']}")
        count = mirror_epss(args.db_dir, force=args.force_full, progress=progress)
        print(f"epss: downloaded={count}" if count >= 0 else "epss: up to date")
        return 0
    if args.command == "ack":
        return _run_ack(args, config)
    specs = _parse_specs(args, config)
    if _warn_stale_feeds(args.db_dir, specs):
        return 1
    acks = load_acks(args.ack_db)
    findings = match_specs(args.db_dir, specs, immutable=args.immutable)
    category = args.category or ""
    if category == "container":
        # ``container`` is a manifest-driven surface, not a package category;
        # a ``--category container`` run is scoped to container findings only.
        findings = []
    else:
        findings = _filtered_findings(findings, args, config, acks)
    if not category or category == "container":
        for container_dir in resolve_container_dirs(args.container_dir):
            container_findings = match_container(
                args.db_dir, container_dir, immutable=args.immutable
            )
            findings.extend(_filter_container_findings(container_findings, config, acks))
    first_seen = first_seen_for(args.state_dir, findings)
    if args.format == "json":
        json.dump({"findings": findings, "first_seen": first_seen}, sys.stdout, indent=2)
        sys.stdout.write("\n")
    elif args.format == "prometheus":
        sys.stdout.write(_render_prometheus(findings, first_seen))
    else:
        sys.stdout.write(_render_table(findings, args.verbose) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
