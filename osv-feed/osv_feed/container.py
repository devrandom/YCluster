"""Container image CVE manifests.

A container manifest is a plain-text file that lists the
``ecosystem:name=version`` specs making up an image's public CVE surface
(base OS packages, Python/Go/npm dependencies, ...). Each line is matched
against the local mirror and the resulting findings carry an ``image``
label derived from the manifest file name, so the same transitive
dependency in two images is tracked and first-seen independently.

Manifest format (one spec per line; ``#`` starts a comment):

    # webapp 0.11.0 (source SHA 0123abc...)
    PyPI:requests=2.32.3
    npm:express=4.19.2
    Go:github.com/example/lib=1.2.3
    Ubuntu:libssl3=3.0.13-1ubuntu0.1
    Alpine:v3.24:gnutls=3.8.13-r0
"""

from __future__ import annotations

import os


class ManifestError(ValueError):
    """Raised when a container manifest cannot be read or parsed."""


def iter_manifest_paths(container_dir: str) -> list[str]:
    """Return the sorted ``*.manifest`` file paths under ``container_dir``."""
    if not os.path.isdir(container_dir):
        return []
    return sorted(
        os.path.join(container_dir, name)
        for name in os.listdir(container_dir)
        if name.endswith(".manifest")
    )


def image_name(path: str) -> str:
    """Return the image name from a manifest path (basename minus suffix)."""
    return os.path.basename(path)[: -len(".manifest")]


def load_manifest(path: str) -> list[dict]:
    """Parse one manifest file into ``{"ecosystem", "package", "version"}`` specs."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError as exc:
        raise ManifestError(f"cannot read container manifest {path}: {exc}") from exc
    specs: list[dict] = []
    for lineno, raw in enumerate(lines, 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        ecosystem, sep, rest = line.rpartition(":")
        name, eq, version = rest.partition("=")
        if not sep or not eq:
            raise ManifestError(
                f"{path}:{lineno}: expected ecosystem:name=version, got {line!r}"
            )
        ecosystem, name, version = (
            ecosystem.strip(),
            name.strip(),
            version.strip(),
        )
        if not ecosystem or not name or not version:
            raise ManifestError(
                f"{path}:{lineno}: expected non-empty ecosystem, name and version"
            )
        specs.append(
            {"ecosystem": ecosystem, "package": name, "version": version}
        )
    return specs


def load_manifests(container_dir: str) -> list[dict]:
    """Load every manifest under ``container_dir``.

    Returns specs with an ``image`` field added from each manifest's file
    name, ready for ``matching.match_specs``.
    """
    result: list[dict] = []
    for path in iter_manifest_paths(container_dir):
        for spec in load_manifest(path):
            entry = dict(spec)
            entry["image"] = image_name(path)
            result.append(entry)
    return result
