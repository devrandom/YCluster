"""Generate ``container/*.manifest`` files from dependency lockfiles.

A container manifest lists the ``ecosystem:name=version`` specs that make
up an image's public CVE surface. osv-feed does not scan images; instead
the manifest is generated deterministically from the pinned source tree we
built, so the tracked surface corresponds exactly to what we run.

Supported inputs (stdlib-only; run at build time from the pinned checkout):

- ``uv.lock`` (uv) -> ``PyPI:name=version`` for every resolved package
- ``package-lock.json`` (npm) -> ``npm:name=version`` for every installed
  package (dependencies and their transitive tree)
- an APK installed database -> ``Alpine:v<release>:origin=version`` using each
  package's ``o:`` source-package origin, not its binary ``P:`` name
- ``go version -m`` buildinfo -> ``Go:module=version``

CLI::

    python3 -m osv_feed.manifestgen webapp \\
      --uv uv.lock --package-lock package-lock.json \\
      --out webapp.manifest

    python3 -m osv_feed.manifestgen forgejo \\
      --apk-installed installed --apk-release v3.24 \\
      --go-buildinfo buildinfo --out forgejo.manifest
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from typing import Iterable

Spec = tuple[str, str]  # (package name, version)


def _sort_specs(specs: Iterable[Spec]) -> list[Spec]:
    return sorted(set(specs))


def _dependency_names(entries) -> set[str]:
    return {d["name"] for d in entries if isinstance(d, dict) and d.get("name")}


def _apk_release(value: str) -> str:
    """Normalize an Alpine release to the OSV ``v<major>.<minor>`` form."""
    release = value.strip()
    if release.startswith("v"):
        release = release[1:]
    parts = release.split(".")
    if len(parts) < 2 or not all(part.isdigit() for part in parts[:2]):
        raise ValueError(f"invalid Alpine release {value!r}")
    return f"v{parts[0]}.{parts[1]}"


def specs_from_apk_installed(path: str, release: str) -> list[Spec]:
    """Return ``(origin, version)`` specs from an APK installed database.

    OSV's Alpine advisories are keyed on the source package. The installed
    database's ``P:`` field is the binary package name, while ``o:`` is the
    source-package origin; subpackages such as ``brotli-libs`` and
    ``openssh-client`` must therefore be emitted under their ``o:`` name.
    Source packages and their subpackages collapse to one spec per origin and
    version.
    """
    _apk_release(release)
    specs: list[Spec] = []
    entry: dict[str, str] = {}

    def flush() -> None:
        package = entry.get("o") or entry.get("P")
        version = entry.get("V")
        if package and version:
            specs.append((package, version))

    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if not line:
                flush()
                entry = {}
                continue
            key, sep, value = line.partition(":")
            if sep:
                entry[key] = value
    flush()
    return _sort_specs(specs)


def specs_from_go_buildinfo(path: str) -> list[Spec]:
    """Return ``(module, version)`` for ``dep`` lines in ``go version -m``."""
    specs: list[Spec] = []
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            fields = raw.strip().split("\t")
            if len(fields) >= 3 and fields[0] == "dep":
                name, version = fields[1], fields[2]
                if name and version:
                    specs.append((name, version))
    return _sort_specs(specs)


def _root_project(data: dict) -> dict | None:
    """Return the uv project root (has dependency groups or is the virtual root)."""
    for pkg in data.get("package", []):
        groups = pkg.get("dev-dependencies")
        if groups is None:
            groups = (pkg.get("metadata") or {}).get("requires-dev")
        source = pkg.get("source")
        if groups or (isinstance(source, dict) and "virtual" in source):
            return pkg
    return None


def _seed_names(root: dict | None, select: str) -> set[str]:
    """Return the direct dependency names to seed the closure with.

    For ``base``, the root project's own ``dependencies`` (the ``[project]
    dependencies`` from pyproject). For a group name, that group's deps from
    ``dev-dependencies`` (falling back to ``metadata.requires-dev``).
    """
    if root is None:
        return set()
    if select == "base":
        return _dependency_names(root.get("dependencies", []))
    groups = root.get("dev-dependencies")
    if groups is None:
        groups = (root.get("metadata") or {}).get("requires-dev") or {}
    return _dependency_names(groups.get(select, []))


def specs_from_uv_lock(path: str, select: str = "all") -> list[Spec]:
    """Return ``(name, version)`` for ``[[package]]`` entries in a uv.lock.

    ``select`` controls which packages are included:

    - ``"all"`` (default): every installable package in the lockfile.
    - ``"base"``: packages reachable from the root project's base
      ``dependencies`` (production deps), excluding dependency groups.
    - ``<group>`` (e.g. ``"prod"``): packages reachable from that
      dependency group's direct deps.

    The production selection (``base`` or a group like ``prod``) excludes
    dev/docs tooling that is never shipped in a runtime image. Packages that
    come from git sources (no ``version``) contribute nothing, and the project
    root itself is never emitted.
    """
    with open(path, "rb") as fh:
        data = tomllib.load(fh)

    by_name: dict[str, list[tuple[str | None, list[str]]]] = {}
    for pkg in data.get("package", []):
        name = pkg.get("name")
        if not name:
            continue
        version = pkg.get("version")
        deps = _dependency_names(pkg.get("dependencies", []))
        by_name.setdefault(name, []).append((version, deps))

    root = _root_project(data)
    if select == "all":
        root_name = root.get("name") if root else None
        included = {name for name in by_name if name != root_name}
    else:
        included: set[str] = set()
        stack = list(_seed_names(root, select))
        while stack:
            name = stack.pop()
            if name in included:
                continue
            included.add(name)
            for version, deps in by_name.get(name, []):
                if version:
                    stack.extend(deps)

    specs: list[Spec] = []
    for name in included:
        for version, _deps in by_name.get(name, []):
            if version:
                specs.append((name, version))
    return _sort_specs(specs)


def _npm_name(key: str) -> str:
    """Derive the package name from a package-lock ``packages`` key.

    Keys look like ``node_modules/@scope/name`` or nested
    ``node_modules/foo/node_modules/bar``; the name is the segment after
    the last ``node_modules/`` (scoped names keep their ``@scope/`` prefix).
    """
    return key.split("node_modules/")[-1]


def specs_from_package_lock(path: str) -> list[Spec]:
    """Return ``(name, version)`` for every installed npm package.

    Handles lockfile v2/v3 (top-level ``packages`` object) and falls back
    to the legacy top-level ``dependencies`` object. The root project entry
    (``""``) and entries without a version (e.g. ``link`` nodes) are skipped.
    """
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    specs: list[Spec] = []
    packages = data.get("packages")
    if isinstance(packages, dict):
        for key, entry in packages.items():
            if not key:
                continue
            if not isinstance(entry, dict):
                continue
            name = _npm_name(key)
            version = entry.get("version")
            if name and version:
                specs.append((name, version))
        if specs:
            return _sort_specs(specs)
    deps = data.get("dependencies")
    if isinstance(deps, dict):
        for name, entry in deps.items():
            if isinstance(entry, dict) and entry.get("version"):
                specs.append((name, entry["version"]))
    return _sort_specs(specs)


def render_manifest_lines(specs_by_ecosystem: Iterable[tuple[str, list[Spec]]]) -> list[str]:
    """Render ``ecosystem:name=version`` lines from per-ecosystem specs."""
    lines: list[str] = []
    for ecosystem, specs in specs_by_ecosystem:
        for name, version in _sort_specs(specs):
            lines.append(f"{ecosystem}:{name}={version}")
    return lines


def generate(
    image: str,
    uv_lock: str | None = None,
    package_lock: str | None = None,
    apk_installed: str | None = None,
    apk_release: str | None = None,
    go_buildinfo: str | None = None,
    comment: str | None = None,
    uv_select: str = "all",
) -> list[str]:
    """Generate manifest lines for ``image`` from the given inputs.

    ``comment`` is emitted as a leading ``#`` line identifying the pin.
    ``uv_select`` restricts ``uv_lock`` to a runtime surface (``all``,
    ``base``, or a group name like ``prod``) so dev-only tooling is not
    shipped in the manifest. Inputs that are not supplied or whose file is
    missing contribute nothing. An APK database requires ``apk_release``.
    """
    if apk_installed and not apk_release:
        raise ValueError("apk_release is required with apk_installed")
    lines: list[str] = []
    if comment:
        lines.append(f"# {comment}".rstrip())
    if uv_lock and os.path.isfile(uv_lock):
        lines.append("# PyPI packages (uv.lock)")
        lines.extend(
            render_manifest_lines(
                [("PyPI", specs_from_uv_lock(uv_lock, select=uv_select))]
            )
        )
    if package_lock and os.path.isfile(package_lock):
        lines.append("# npm packages (package-lock.json)")
        lines.extend(
            render_manifest_lines([("npm", specs_from_package_lock(package_lock))])
        )
    if apk_installed and os.path.isfile(apk_installed):
        lines.append("# Alpine packages (apk installed DB, source origins)")
        lines.extend(
            render_manifest_lines(
                [
                    (
                        f"Alpine:{_apk_release(apk_release or '')}",
                        specs_from_apk_installed(apk_installed, apk_release or ""),
                    )
                ]
            )
        )
    if go_buildinfo and os.path.isfile(go_buildinfo):
        lines.append("# Go modules (go version -m)")
        lines.extend(
            render_manifest_lines(
                [("Go", specs_from_go_buildinfo(go_buildinfo))]
            )
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="osv-feed-manifestgen", description=__doc__
    )
    parser.add_argument("image", help="image name (used for the manifest file name)")
    parser.add_argument("--uv", help="path to uv.lock")
    parser.add_argument(
        "--uv-select",
        default="all",
        help="uv packages to include: all, base, or a dependency group (e.g. prod)",
    )
    parser.add_argument("--package-lock", help="path to package-lock.json")
    parser.add_argument("--apk-installed", help="path to an APK installed database")
    parser.add_argument(
        "--apk-release",
        help="Alpine release for APK specs, e.g. v3.24 or 3.24.2",
    )
    parser.add_argument(
        "--go-buildinfo",
        help="path to `go version -m` buildinfo output",
    )
    parser.add_argument("--comment", help="leading # comment describing the pin")
    parser.add_argument("--out", help="write manifest here (default: <image>.manifest)")
    args = parser.parse_args(argv)
    if args.apk_installed and not args.apk_release:
        parser.error("--apk-release is required with --apk-installed")

    lines = generate(
        args.image,
        uv_lock=args.uv,
        package_lock=args.package_lock,
        apk_installed=args.apk_installed,
        apk_release=args.apk_release,
        go_buildinfo=args.go_buildinfo,
        comment=args.comment,
        uv_select=args.uv_select,
    )
    out = args.out or f"{args.image}.manifest"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"wrote {out}: {len(lines)} lines", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
