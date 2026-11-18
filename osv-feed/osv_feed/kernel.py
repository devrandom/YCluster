"""Compact index for distro kernel advisories.

Ubuntu files every kernel CVE against the ``linux`` source package and the
per-series HWE packages, and each advisory lists every affected binary
version across every series: ~19.5k records and ~5.5 GB of JSON for the
fleet's kernel surface. Matching a running kernel against the mirror parses
all of it, which costs minutes.

The index keeps one entry per (package, series, CVE): the first affected
version, the fixed version when one exists, and the advisory fields a
reporter needs. Series is the version prefix before the first ``-``
(``6.8.0`` in ``6.8.0-149.149``), which is what separates the per-series
affected-version lists inside a single record. Matching then costs a handful
of integer comparisons.
"""

from __future__ import annotations

import json
import os
import re
from typing import Iterable, Iterator

from .matching import FIX_AVAILABLE
from .matching import NO_FIX_YET
from .matching import _severity_label
from .matching import version_compare

INDEX_FORMAT = 1

# Ubuntu kernel versions look like 6.8.0-149.149 or, for HWE uploads,
# 7.0.0-43.43~24.04.1. Debian compares the tilde part as *before* the plain
# version, which the trailing rank reproduces.
_KERNEL_VERSION = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)-(\d+)\.(\d+)(?:~(\d+)\.(\d+)\.(\d+))?$"
)


def series_of(version: str) -> str | None:
    """Return the version series (``6.8.0``) or None when there is none."""
    if not version or version == "0":
        return None
    head = version.split("-", 1)[0]
    if not head or not head[0].isdigit():
        return None
    return head


def version_key(version: str) -> tuple | None:
    """Sortable key for an Ubuntu kernel version, or None for other forms.

    Only versions in the same series are ever compared through this key (the
    caller filters by series first), so the leading A.B.C is redundant but
    harmless.
    """
    match = _KERNEL_VERSION.match(version)
    if match is None:
        return None
    major, minor, patch, abi, upload, hwe_major, hwe_minor, hwe_patch = (
        match.groups()
    )
    # `1 X.Y.Z` sorts a plain upload after the same build with a ~suffix,
    # matching dpkg's tilde ordering.
    return (
        int(major),
        int(minor),
        int(patch),
        int(abi),
        int(upload),
        1 if hwe_major is None else 0,
        int(hwe_major or 0),
        int(hwe_minor or 0),
        int(hwe_patch or 0),
    )


def _compare(left: str, right: str) -> int:
    left_key = version_key(left)
    right_key = version_key(right)
    if left_key is not None and right_key is not None:
        return (left_key > right_key) - (left_key < right_key)
    return version_compare(left, right)


def build_entries(record: dict, packages: set[str]) -> list[dict]:
    """Reduce one OSV record to per-(package, series) advisory entries."""

    def _has_fixed_range(affected: dict) -> bool:
        for rng in affected.get("ranges") or []:
            for event in rng.get("events") or []:
                if "fixed" in event:
                    return True
        return False

    def _store_entry(store: dict, entry: dict) -> None:
        key = (entry["package"], entry.get("series"))
        previous = store.get(key)
        # An entry that knows about a fix supersedes an open-ended one for
        # the same series.
        if previous is None or (not previous.get("has_fix") and entry.get("has_fix")):
            store[key] = entry

    cve = str(record.get("id") or "")
    if not cve:
        return []
    summary = str(record.get("summary") or "")
    aliases = [str(alias) for alias in record.get("aliases") or []]
    published = str(record.get("published") or "")
    entries: dict[tuple[str, str | None], dict] = {}
    for affected in record.get("affected") or []:
        package = str((affected.get("package") or {}).get("name") or "").lower()
        if package not in packages:
            continue
        severity = _severity_label(record, package)
        versions = affected.get("versions") or []
        if versions:
            # A version list is the authoritative affected set. Ubuntu
            # publishes it ascending per series, so the first and last
            # occurrence of each series bound the window; the range events
            # only say whether a fix exists somewhere in the advisory.
            windows: dict[str | None, list[str]] = {}
            for version in versions:
                window = windows.setdefault(series_of(version), [version, version])
                window[1] = version
            for series, (first, last) in windows.items():
                entry = {
                    "package": package,
                    "series": series,
                    "cve": cve,
                    "severity": severity,
                    "first": first,
                    "last": last,
                    "fixed": None,
                    "mode": "versions",
                    "has_fix": _has_fixed_range(affected),
                    "summary": summary,
                    "aliases": aliases,
                    "published": published,
                }
                _store_entry(entries, entry)
            continue

        # No version list: classify from the range events. An open-ended
        # `introduced` with no fixed/last_affected has no fix yet.
        for rng in affected.get("ranges") or []:
            if rng.get("type") not in ("ECOSYSTEM", "SEMVER"):
                continue
            introduced: str | None = None
            for event in rng.get("events") or []:
                entry = None
                if "introduced" in event:
                    introduced = event["introduced"]
                elif "fixed" in event:
                    entry = {
                        "first": introduced,
                        "last": None,
                        "fixed": event["fixed"],
                        "mode": "range",
                        "series": series_of(event["fixed"])
                        or series_of(introduced or ""),
                    }
                    introduced = None
                elif "last_affected" in event:
                    entry = {
                        "first": introduced,
                        "last": event["last_affected"],
                        "fixed": None,
                        "mode": "range",
                        "series": series_of(event["last_affected"])
                        or series_of(introduced or ""),
                    }
                    introduced = None
                if entry is not None:
                    entry.update(
                        {
                            "package": package,
                            "cve": cve,
                            "severity": severity,
                            "has_fix": "fixed" in event,
                            "summary": summary,
                            "aliases": aliases,
                            "published": published,
                        }
                    )
                    _store_entry(entries, entry)
            if introduced is not None:
                _store_entry(
                    entries,
                    {
                        "package": package,
                        "series": series_of(introduced),
                        "cve": cve,
                        "severity": severity,
                        "first": introduced,
                        "last": None,
                        "fixed": None,
                        "mode": "range",
                        "has_fix": False,
                        "summary": summary,
                        "aliases": aliases,
                        "published": published,
                    },
                )
    return list(entries.values())


def build_index(records: Iterable[dict], packages: Iterable[str]) -> list[dict]:
    """Build index entries for every record mentioning one of ``packages``."""
    wanted = {package.lower() for package in packages}
    entries: list[dict] = []
    for record in records:
        entries.extend(build_entries(record, wanted))
    return entries


def group(entries: Iterable[dict]) -> dict[tuple[str, str | None], list[dict]]:
    """Group entries by (package, series) for per-version lookups."""
    grouped: dict[tuple[str, str | None], list[dict]] = {}
    for entry in entries:
        grouped.setdefault((entry["package"], entry.get("series")), []).append(entry)
    return grouped


def classify(
    candidates: Iterable[dict], version: str
) -> Iterator[tuple[dict, str]]:
    """Yield (entry, state) for entries that apply to ``version``.

    ``mode="versions"`` entries come from an explicit affected-version list:
    the version must fall inside the listed window for its series, and the
    range events only decide whether a fix exists. ``mode="range"`` entries
    come from introduced/fixed/last_affected events and are compared
    directly.
    """
    states: dict[str, tuple[dict, str]] = {}
    for entry in candidates:
        first = entry.get("first")
        if first not in (None, "0") and _compare(version, first) < 0:
            continue
        last = entry.get("last")
        fixed = entry.get("fixed")
        if entry.get("mode") == "versions":
            if _compare(version, last) > 0:
                continue
            state = FIX_AVAILABLE if entry.get("has_fix") else NO_FIX_YET
        elif fixed is not None:
            if _compare(version, fixed) >= 0:
                continue
            state = FIX_AVAILABLE
        elif last is not None:
            if _compare(version, last) > 0:
                continue
            state = NO_FIX_YET
        else:
            state = NO_FIX_YET
        cve = entry["cve"]
        previous = states.get(cve)
        if previous is None or (previous[1] == NO_FIX_YET and state == FIX_AVAILABLE):
            states[cve] = (entry, state)
    return iter(states.values())


def match_entries(
    grouped: dict[tuple[str, str | None], list[dict]],
    package: str,
    version: str,
) -> list[tuple[dict, str]]:
    """Classify one package/version against a grouped index."""
    candidates = grouped.get((package.lower(), series_of(version)), [])
    return list(classify(candidates, version))


def dump_index(
    path: str, ecosystem: str, packages: Iterable[str], entries: Iterable[dict]
) -> int:
    """Write the index atomically; returns the number of entries written."""
    payload = {
        "format": INDEX_FORMAT,
        "ecosystem": ecosystem,
        "packages": sorted({package.lower() for package in packages}),
        "entries": list(entries),
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, path)
    return len(payload["entries"])


def load_index(path: str) -> tuple[str, dict[tuple[str, str | None], list[dict]]]:
    """Load an index file and return its ecosystem plus the grouped entries."""
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("format") != INDEX_FORMAT:
        raise ValueError(
            f"unsupported kernel index format {payload.get('format')!r} in {path}"
        )
    return str(payload.get("ecosystem") or ""), group(payload.get("entries") or [])


__all__ = [
    "INDEX_FORMAT",
    "build_entries",
    "build_index",
    "classify",
    "dump_index",
    "group",
    "load_index",
    "match_entries",
    "series_of",
    "version_key",
]
