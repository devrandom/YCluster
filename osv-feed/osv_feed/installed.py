"""Resolve installed package versions from the local host.

Debian-family hosts can answer the installed version for a binary package
or a source package name (any installed binary built from that source).
This lets `osv-feed match` use installed versions when the caller omits
them, and lets binary package names be mapped back to the source package
name OSV distro records use, without hardcoding host facts anywhere.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class InstalledPackage:
    """An installed package resolved from dpkg.

    ``name`` is the source package name when the requested binary differs
    from its source (for example ``openssh-client`` resolves to ``openssh``);
    otherwise it is the requested name.
    """

    name: str
    version: str


class PackageNotInstalledError(LookupError):
    """Raised when a requested package is not installed and no version was given."""


def host_ecosystem() -> str | None:
    """Return the OSV ecosystem name for the host OS.

    Returns ``Ubuntu`` or ``Debian`` when the host's ``/etc/os-release``
    ``ID`` is one of those, and ``None`` for any other distribution (where
    the caller must supply an ecosystem explicitly).
    """
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        return None
    return _parse_os_release(content)


def _parse_os_release(content: str) -> str | None:
    distro_id = ""
    for line in content.splitlines():
        if line.startswith("ID="):
            distro_id = line.partition("=")[2].strip().strip('"').lower()
            break
    if distro_id == "ubuntu":
        return "Ubuntu"
    if distro_id == "debian":
        return "Debian"
    return None


def _query() -> str:
    try:
        proc = subprocess.run(
            ["dpkg-query", "-W", "-f", "${Package} ${Source:Package} ${Version}\n"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise LookupError(f"cannot resolve installed version: {exc}") from exc
    if proc.returncode != 0:
        raise LookupError(
            f"cannot read installed package versions: "
            f"{proc.stderr.strip() or proc.returncode}"
        )
    return proc.stdout


def installed_package(package: str) -> InstalledPackage:
    """Return the installed ``InstalledPackage`` for ``package``.

    Prefers an exact binary-package match, then any installed binary whose
    source package name matches. The returned name is the source package
    name when known, so binary names map to the distro CVE package. Raises
    LookupError when the package is not installed or the host has no dpkg.
    """
    exact_name: str | None = None
    exact_version: str | None = None
    source_version: str | None = None
    for line in _query().splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        pkg, source, version = fields
        if pkg == package:
            exact_name = source or package
            exact_version = version
        elif source == package and source_version is None:
            source_version = version
    if exact_name is not None and exact_version is not None:
        return InstalledPackage(exact_name, exact_version)
    if source_version is not None:
        return InstalledPackage(package, source_version)
    raise PackageNotInstalledError(f"{package!r} is not installed and no version was given")


def installed_version(package: str) -> str:
    """Return the installed version of ``package``.

    Prefers an exact binary-package match, then any installed binary whose
    source package name matches. Raises LookupError when the package is not
    installed or the host has no dpkg.
    """
    return installed_package(package).version
