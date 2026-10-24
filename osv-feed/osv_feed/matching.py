"""Match OSV records against package versions using a local mirror.

Implements the OSV range semantics needed by distro ecosystems: a package
is affected when the version falls inside an ECOSYSTEM/SEMVER range
(introduced / fixed / last_affected events). Debian-family comparison uses
`dpkg --compare-versions` when available, with a numeric-run fallback key
for generic environments. Go and Alpine use their own version ordering so
`v`-prefixed semver and apk suffixes are compared correctly.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
import time
from itertools import zip_longest
from typing import Callable

import sqlite3

from .container import load_manifests
from .epss import load_epss
from .mirror import connect
from .mirror import db_path

VersionCompare = Callable[[str, str], int]

PATCHED = "patched"
FIX_AVAILABLE = "fix_available"
NO_FIX_YET = "no_fix_yet"
UNINSTALLED = "uninstalled"

STATE_ORDER = {NO_FIX_YET: 0, FIX_AVAILABLE: 1, PATCHED: 2}


def _have_dpkg() -> bool:
    try:
        proc = subprocess.run(
            ["dpkg", "--version"], capture_output=True, check=False
        )
        return proc.returncode == 0
    except OSError:
        return False


_DPKG_AVAILABLE = _have_dpkg()

_COMPARE_CACHE: dict[tuple[str, str], int] = {}


def version_compare(a: str, b: str) -> int:
    """Return -1/0/1 comparing two version strings.

    Uses `dpkg --compare-versions` for Debian-family semantics when dpkg is
    installed and a numeric-run fallback key otherwise. The fallback is
    intentionally best-effort; Debian/Ubuntu callers should use dpkg.
    Results are cached because OSV records repeat the same introduced/fixed
    versions across many CVEs.
    """
    if a == b:
        return 0
    key = (a, b)
    cached = _COMPARE_CACHE.get(key)
    if cached is not None:
        return cached
    if _DPKG_AVAILABLE:
        for op, result in (("lt", -1), ("gt", 1)):
            proc = subprocess.run(
                ["dpkg", "--compare-versions", a, op, b],
                capture_output=True,
                check=False,
            )
            if proc.returncode == 0:
                _COMPARE_CACHE[key] = result
                return result
        _COMPARE_CACHE[key] = 0
        return 0
    a_key = _fallback_key(a)
    b_key = _fallback_key(b)
    result = (a_key > b_key) - (a_key < b_key)
    _COMPARE_CACHE[key] = result
    return result


def _fallback_key(version: str) -> tuple:
    # Split into [(text, number), ...] so 9.10 > 9.6 and epochs sort first.
    parts: list = []
    for piece in re.split(r"(\d+)", version.lower()):
        parts.append(int(piece) if piece.isdigit() else piece)
    return tuple(parts)


def _parse_semver(version: str) -> tuple[list[int], list[str]]:
    value = version.strip()
    if value.startswith("v"):
        value = value[1:]
    value = value.split("+", 1)[0]
    core, _, prerelease = value.partition("-")
    numbers = []
    for part in core.split("."):
        numbers.append(int(part) if part.isdigit() else 0)
    while len(numbers) < 3:
        numbers.append(0)
    return numbers, prerelease.split(".") if prerelease else []


def go_version_compare(a: str, b: str) -> int:
    """Compare Go module versions (``v``-prefixed semver)."""
    if a == b:
        return 0
    a_nums, a_pre = _parse_semver(a)
    b_nums, b_pre = _parse_semver(b)
    if a_nums != b_nums:
        return (a_nums > b_nums) - (a_nums < b_nums)
    if not a_pre and not b_pre:
        return 0
    if not a_pre:
        return 1
    if not b_pre:
        return -1
    for left, right in zip_longest(a_pre, b_pre):
        if left is None:
            return -1
        if right is None:
            return 1
        left_num, right_num = left.isdigit(), right.isdigit()
        if left_num and right_num:
            if int(left) != int(right):
                return (int(left) > int(right)) - (int(left) < int(right))
        elif left_num:
            return -1
        elif right_num:
            return 1
        elif left != right:
            return (left > right) - (left < right)
    return 0


_APK_SUFFIX = {
    "alpha": 0,
    "beta": 1,
    "pre": 2,
    "rc": 3,
    "cvs": 5,
    "svn": 6,
    "git": 7,
    "hg": 8,
    "p": 9,
}
_APK_NONE = 4


def _apk_components(version: str) -> list[tuple[int, int]]:
    value = version.strip().lower()
    if ":" in value:
        _epoch, value = value.split(":", 1)
    if "-r" in value:
        value, _revision = value.rsplit("-r", 1)
    parts: list[tuple[int, int]] = []
    for part in re.split(r"[._]", value):
        match = re.fullmatch(r"([a-z]+)(\d*)", part)
        if match and match.group(1) in _APK_SUFFIX:
            parts.append((1, _APK_SUFFIX[match.group(1)] * 100000 + int(match.group(2) or 0)))
        elif part.isdigit():
            parts.append((0, int(part)))
        else:
            parts.append((0, 0))
    return parts


def alpine_version_compare(a: str, b: str) -> int:
    """Compare Alpine apk versions, including suffixes and ``-r`` revisions."""
    if a == b:
        return 0

    def split(value: str) -> tuple[int, list[tuple[int, int]], int]:
        epoch = 0
        if ":" in value:
            epoch_text, value = value.split(":", 1)
            epoch = int(epoch_text) if epoch_text.isdigit() else 0
        revision = 0
        if "-r" in value:
            value, revision_text = value.rsplit("-r", 1)
            revision = int(revision_text) if revision_text.isdigit() else 0
        return epoch, _apk_components(value), revision

    a_epoch, a_parts, a_rev = split(a)
    b_epoch, b_parts, b_rev = split(b)
    if a_epoch != b_epoch:
        return (a_epoch > b_epoch) - (a_epoch < b_epoch)
    for left, right in zip_longest(a_parts, b_parts):
        if left is None:
            if right[0] == 0:
                return -1
            return 1 if right[1] < _APK_NONE * 100000 else -1
        if right is None:
            if left[0] == 0:
                return 1
            return -1 if left[1] < _APK_NONE * 100000 else 1
        if left != right:
            return (left > right) - (left < right)
    return (a_rev > b_rev) - (a_rev < b_rev)


def compare_for_ecosystem(ecosystem: str, default: VersionCompare) -> VersionCompare:
    if ecosystem == "Go":
        return go_version_compare
    if ecosystem == "Alpine" or ecosystem.startswith("Alpine:"):
        return alpine_version_compare
    return default


def iter_records(db_dir: str, ecosystem: str):
    """Yield every OSV record for ``ecosystem`` from the SQLite store."""
    try:
        conn = connect(db_dir, read_only=True)
    except sqlite3.OperationalError:
        return
    try:
        rows = conn.execute(
            "SELECT data FROM records WHERE ecosystem=? ORDER BY id", (ecosystem,)
        )
        for row in rows:
            try:
                yield json.loads(row[0])
            except ValueError as exc:
                raise ValueError(
                    f"cannot read OSV record in {db_path(db_dir)}: {exc}"
                ) from exc
    finally:
        conn.close()


def _candidate_records(
    db_dir: str, ecosystem: str, packages: list[str], *, immutable: bool = False
) -> list[dict]:
    """Load only records that mention any requested package.

    Uses the in-database package index so only candidate records are read,
    regardless of how many ecosystems share the store.
    """
    wanted = [package.lower() for package in packages]
    if not wanted:
        return []
    placeholders = ",".join("?" for _ in wanted)
    sql = (
        "SELECT DISTINCT r.data FROM records r "
        "JOIN packages p ON p.ecosystem = r.ecosystem AND p.record_id = r.id "
        f"WHERE r.ecosystem=? AND p.package IN ({placeholders})"
    )
    try:
        conn = connect(db_dir, read_only=True, immutable=immutable)
    except sqlite3.OperationalError:
        return []
    try:
        return [
            json.loads(row[0])
            for row in conn.execute(sql, [ecosystem, *wanted]).fetchall()
        ]
    finally:
        conn.close()


def _range_fix_status(
    affected: dict, version: str, cmp: VersionCompare
) -> bool | None:
    """Classify one affected entry for ``version``.

    Returns True when the version is affected and a fix is available, False
    when it is affected with no fix yet, and None when it is not affected.
    ``events`` are evaluated sequentially so that multiple disjoint
    sub-ranges inside a single range are honored, and the fix status is taken
    from the sub-range that actually contained ``version`` rather than any
    other range in the same entry.
    """
    versions = affected.get("versions")
    if versions:
        return _has_fixed(affected) if version in versions else None

    for rng in affected.get("ranges") or []:
        if rng.get("type") not in ("ECOSYSTEM", "SEMVER"):
            continue
        introduced: str | None = None
        for event in rng.get("events") or []:
            if "introduced" in event:
                introduced = event["introduced"]
            elif "fixed" in event or "last_affected" in event:
                lower = introduced if introduced is not None else "0"
                if "fixed" in event:
                    if cmp(version, lower) >= 0 and cmp(version, event["fixed"]) < 0:
                        return True
                elif (
                    cmp(version, lower) >= 0
                    and cmp(version, event["last_affected"]) <= 0
                ):
                    return False
                introduced = None
        # A trailing ``introduced`` with no following ``fixed``/``last_affected``
        # is an open-ended range with no fix yet.
        if introduced is not None and cmp(version, introduced) >= 0:
            return False
    return None


def _has_fixed(affected: dict) -> bool:
    for rng in affected.get("ranges") or []:
        for event in rng.get("events") or []:
            if "fixed" in event:
                return True
    return False


def _affected_entries(
    record: dict, package: str, ecosystem: str | None = None
) -> list[dict]:
    wanted = package.lower()
    wanted_ecosystem = ecosystem.lower() if ecosystem else None
    entries = [
        entry
        for entry in record.get("affected") or []
        if (entry.get("package") or {}).get("name", "").lower() == wanted
    ]
    if wanted_ecosystem and ":" in wanted_ecosystem:
        # Alpine records carry one affected entry per release
        # (``Alpine:v3.24``); a base entry applies to every release.
        entries = [
            entry
            for entry in entries
            if (entry.get("package") or {}).get("ecosystem", "").lower()
            in (wanted_ecosystem, wanted_ecosystem.split(":", 1)[0])
        ]
    return entries


def _cvss3_qualitative(vector: str) -> str | None:
    """Return low/medium/high/critical from a CVSS 3.x vector."""
    if not vector.startswith("CVSS:3."):
        return None
    metrics: dict[str, str] = {}
    for part in vector.split("/")[1:]:
        key, sep, value = part.partition(":")
        if sep:
            metrics[key] = value
    try:
        av = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}[metrics["AV"]]
        ac = {"L": 0.77, "H": 0.44}[metrics["AC"]]
        scope = metrics.get("S", "U")
        pr = {
            "U": {"N": 0.85, "L": 0.62, "H": 0.27},
            "C": {"N": 0.85, "L": 0.68, "H": 0.5},
        }[scope][metrics["PR"]]
        ui = {"N": 0.85, "R": 0.62}[metrics["UI"]]
        c = {"H": 0.56, "L": 0.22, "N": 0.0}[metrics["C"]]
        i = {"H": 0.56, "L": 0.22, "N": 0.0}[metrics["I"]]
        a = {"H": 0.56, "L": 0.22, "N": 0.0}[metrics["A"]]
    except KeyError:
        return None
    iss = 1.0 - (1.0 - c) * (1.0 - i) * (1.0 - a)
    exploitability = 8.22 * av * ac * pr * ui
    if scope == "U":
        impact = 6.42 * iss
        base_score = impact + exploitability
    else:
        impact = 7.52 * (iss - 0.029) - 3.25 * ((iss - 0.02) ** 15)
        base_score = min(1.08 * (impact + exploitability), 10.0)
    if impact <= 0:
        return None
    score = math.ceil(base_score * 10) / 10
    if score < 4.0:
        return "low"
    if score < 7.0:
        return "medium"
    if score < 9.0:
        return "high"
    return "critical"


def _cvss4_qualitative(vector: str) -> str | None:
    """Qualitative label for a vector-only CVSS 4.0 record.

    The official CVSS 4.0 base score needs the FIRST macrovector lookup; when
    an advisory publishes only the vector (as the Alpine OSV records do), use the
    impact and exploitability metrics. Critical requires both confidentiality
    and integrity impact (code/data compromise) at network reachability; an
    availability-only DoS is high, not critical.
    """
    if not vector.startswith("CVSS:4."):
        return None
    metrics: dict[str, str] = {}
    for part in vector.split("/")[1:]:
        key, sep, value = part.partition(":")
        if sep:
            metrics[key] = value
    primary = [metrics.get(key, "N") for key in ("VC", "VI", "VA")]
    impacts = primary + [metrics.get(key, "N") for key in ("SC", "SI", "SA")]
    if not any(level in ("H", "L") for level in impacts):
        return "low"
    if (
        metrics.get("VC") == "H"
        and metrics.get("VI") == "H"
        and metrics.get("AV") == "N"
        and metrics.get("AC") == "L"
        and metrics.get("PR") == "N"
        and metrics.get("UI") == "N"
    ):
        return "critical"
    if "H" in impacts:
        return "high"
    return "medium"


# CVSS 4.0 has no stable vector-only qualitative rating (the base score uses
# a macrovector lookup), so CVSS 4.0 records prefer the advisory's own
# qualitative label published in OSV ``database_specific.severity`` (GitHub's
# rating on GHSA/Go records), same trust model as Debian urgency / Ubuntu
# priority labels. Alpine records publish only the vector, so fall back to the
# conservative impact/exploitability mapping above.
_GHSA_SEVERITY = {
    "CRITICAL": "critical",
    "HIGH": "high",
    "MODERATE": "medium",
    "LOW": "low",
}


def _osv_db_qualitative(record: dict) -> str | None:
    """Return the OSV ``database_specific.severity`` as a canonical label."""
    value = (record.get("database_specific") or {}).get("severity")
    if not value:
        return None
    label = _GHSA_SEVERITY.get(str(value).upper())
    if label is not None:
        return label
    return str(value).lower()


def _severity_label(record: dict, package: str) -> str:
    """Best human severity for a package finding.

    Prefers distro-specific labels (Debian ``urgency``, Ubuntu priority
    scores, OSV ``database_specific.severity`` for CVSS 4.0/GHSA records) and
    falls back to the CVSS 3.x qualitative rating.
    """
    distro: list[str] = []
    cvss: list[str] = []

    def collect(entry: dict) -> None:
        for sev in entry.get("severity") or []:
            score = sev.get("score")
            if not score:
                continue
            severity_type = (sev.get("type") or "").upper()
            if severity_type == "CVSS_V3":
                qual = _cvss3_qualitative(str(score))
            elif severity_type == "CVSS_V4":
                qual = _cvss4_qualitative(str(score))
            else:
                qual = None
            if qual:
                if qual not in cvss:
                    cvss.append(qual)
            elif str(score) not in distro:
                distro.append(str(score))

    db_qual = _osv_db_qualitative(record)
    if db_qual and db_qual not in distro:
        distro.append(db_qual)
    for entry in _affected_entries(record, package):
        urgency = (entry.get("ecosystem_specific") or {}).get("urgency")
        if urgency and str(urgency).lower() not in ("not yet assigned", "end-of-life"):
            if str(urgency) not in distro:
                distro.append(str(urgency))
        collect(entry)
    collect(record)
    return distro[0] if distro else (cvss[0] if cvss else "")


def classify_record(
    record: dict,
    package: str,
    version: str,
    cmp: VersionCompare = version_compare,
    ecosystem: str | None = None,
) -> str:
    """Classify one record/package/version as patched, fix_available, or
    no_fix_yet. Records that do not mention the package are patched."""
    state = PATCHED
    for entry in _affected_entries(record, package, ecosystem):
        status = _range_fix_status(entry, version, cmp)
        if status is True:
            return FIX_AVAILABLE
        if status is False:
            return NO_FIX_YET
    return state


def _first_seen(db_dir: str, findings: list[dict]) -> dict[str, int]:
    os.makedirs(db_dir, exist_ok=True)
    state_path = os.path.join(db_dir, "first-seen.json")
    try:
        with open(state_path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except (FileNotFoundError, ValueError):
        state = {}
    now = int(time.time())
    result: dict[str, int] = {}
    current_keys = set()
    for finding in findings:
        key = _finding_key(finding)
        current_keys.add(key)
        if key not in state:
            state[key] = now
        result[key] = state[key]
    state = {key: value for key, value in state.items() if key in current_keys}
    fd, tmp = tempfile.mkstemp(dir=db_dir, prefix=".first-seen-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, sort_keys=True)
    except Exception:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        raise
    os.replace(tmp, state_path)
    return result


def _finding_key(finding: dict) -> str:
    if finding.get("image"):
        return "|".join(
            (
                "image",
                finding["image"],
                finding["ecosystem"],
                finding["requested_package"],
                finding["version"],
                finding["cve"],
            )
        )
    return "|".join(
        (finding["ecosystem"], finding["requested_package"], finding["version"], finding["cve"])
    )


PENDING_STATES = (NO_FIX_YET, FIX_AVAILABLE)


def first_seen_for(db_dir: str, findings: list[dict]) -> dict[str, int]:
    """Return persisted first-seen epochs for pending (unresolved) findings.

    Only findings in a pending state (``no_fix_yet`` / ``fix_available``) are
    tracked, so patched, uninstalled, or category-filtered-out entries are
    pruned and never contribute to the "pending longer than N days" alert.
    """
    return _first_seen(
        db_dir, [finding for finding in findings if finding["state"] in PENDING_STATES]
    )


def _epss_for(
    epss: dict[str, dict[str, float]], record: dict
) -> dict[str, float] | None:
    """Return the EPSS entry for a record, checking its id then aliases."""
    candidates = [record.get("id", "")] + [
        str(alias) for alias in (record.get("aliases") or [])
    ]
    for candidate in candidates:
        hit = epss.get(candidate)
        if hit is not None:
            return hit
    return None


def match_specs(
    db_dir: str,
    specs: list[dict],
    cmp: VersionCompare = version_compare,
    *,
    immutable: bool = False,
) -> list[dict]:
    """Match package specs against the local mirror.

    specs items: {"ecosystem": str, "package": str, "version": str}
    Returns the findings list; first-seen state is managed separately by
    ``first_seen_for`` after any filtering has been applied.

    ``immutable`` opens the store without locking and without writing
    journal files — intended for read-only consumers pointed at an atomic
    snapshot (e.g. on a network share).
    """
    epss = load_epss(db_dir)
    by_ecosystem: dict[str, list[dict]] = {}
    for spec in specs:
        by_ecosystem.setdefault(spec["ecosystem"], []).append(spec)

    findings: list[dict] = []
    for ecosystem, ecosystem_specs in by_ecosystem.items():
        ecosystem_cmp = compare_for_ecosystem(ecosystem, cmp)
        base_ecosystem = ecosystem.split(":", 1)[0] if ecosystem.startswith("Alpine:") else ecosystem
        records = _candidate_records(
            db_dir,
            base_ecosystem,
            [spec["package"] for spec in ecosystem_specs],
            immutable=immutable,
        )
        for spec in ecosystem_specs:
            if spec.get("version") is None:
                finding = {
                    "ecosystem": ecosystem,
                    "package": spec["package"],
                    "requested_package": spec["package"],
                    "version": "",
                    "cve": "",
                    "summary": "",
                    "details": "",
                    "aliases": [],
                    "references": [],
                    "published": "",
                    "modified": "",
                    "severity": "",
                    "state": UNINSTALLED,
                    "epss_score": 0.0,
                    "epss_percentile": 0.0,
                }
                if spec.get("image"):
                    finding["image"] = spec["image"]
                findings.append(finding)
                continue
            for record in records:
                if not _affected_entries(record, spec["package"], ecosystem):
                    continue
                epss_hit = _epss_for(epss, record)
                finding = {
                    "ecosystem": ecosystem,
                    "package": spec["package"],
                    "requested_package": spec["package"],
                    "version": spec["version"],
                    "cve": record.get("id", ""),
                    "summary": record.get("summary", ""),
                    "details": record.get("details", ""),
                    "aliases": [str(a) for a in (record.get("aliases") or [])],
                    "references": [
                        r.get("url")
                        for r in (record.get("references") or [])
                        if isinstance(r, dict) and r.get("url")
                    ],
                    "published": record.get("published", ""),
                    "modified": record.get("modified", ""),
                    "severity": _severity_label(record, spec["package"]),
                    "state": classify_record(
                        record,
                        spec["package"],
                        spec["version"],
                        ecosystem_cmp,
                        ecosystem,
                    ),
                    "epss_score": epss_hit["score"] if epss_hit else 0.0,
                    "epss_percentile": (
                        epss_hit["percentile"] if epss_hit else 0.0
                    ),
                }
                if spec.get("image"):
                    finding["image"] = spec["image"]
                findings.append(finding)
    findings.sort(
        key=lambda f: (
            f["ecosystem"],
            f["requested_package"],
            STATE_ORDER.get(f["state"], 9),
            f["cve"],
        )
    )
    return findings


def match_container(
    db_dir: str,
    container_dir: str,
    cmp: VersionCompare = version_compare,
    *,
    immutable: bool = False,
) -> list[dict]:
    """Match every ``ecosystem:name=version`` spec in ``container_dir``.

    Loads each ``*.manifest`` file and matches it through the same pipeline
    as host packages; each finding carries an ``image`` label from its
    manifest file name so first-seen and alerting are per-image. Returns an
    empty list when the directory contains no manifests.
    """
    manifests = load_manifests(container_dir)
    if not manifests:
        return []
    return match_specs(db_dir, manifests, cmp=cmp, immutable=immutable)
