"""Match OSV records against package versions using a local mirror.

Implements the OSV range semantics needed by distro ecosystems: a package
is affected when the version falls inside an ECOSYSTEM/SEMVER range
(introduced / fixed / last_affected events). Debian-family comparison uses
`dpkg --compare-versions` when available, with a numeric-run fallback key
for generic environments.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
import time
from typing import Callable

import sqlite3

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


def _affected_entries(record: dict, package: str) -> list[dict]:
    wanted = package.lower()
    return [
        entry
        for entry in record.get("affected") or []
        if (entry.get("package") or {}).get("name", "").lower() == wanted
    ]


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


def _severity_label(record: dict, package: str) -> str:
    """Best human severity for a package finding.

    Prefers distro-specific labels (Debian ``urgency``, Ubuntu priority
    scores) and falls back to the CVSS 3.x qualitative rating.
    """
    distro: list[str] = []
    cvss: list[str] = []

    def collect(entry: dict) -> None:
        for sev in entry.get("severity") or []:
            score = sev.get("score")
            if not score:
                continue
            if (sev.get("type") or "").upper() in ("CVSS_V3", "CVSS_V4"):
                qual = _cvss3_qualitative(str(score))
                if qual and qual not in cvss:
                    cvss.append(qual)
            elif str(score) not in distro:
                distro.append(str(score))

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
) -> str:
    """Classify one record/package/version as patched, fix_available, or
    no_fix_yet. Records that do not mention the package are patched."""
    state = PATCHED
    for entry in _affected_entries(record, package):
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
        records = _candidate_records(
            db_dir,
            ecosystem,
            [spec["package"] for spec in ecosystem_specs],
            immutable=immutable,
        )
        for spec in ecosystem_specs:
            if spec.get("version") is None:
                findings.append(
                    {
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
                )
                continue
            for record in records:
                if not _affected_entries(record, spec["package"]):
                    continue
                epss_hit = _epss_for(epss, record)
                findings.append(
                    {
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
                            record, spec["package"], spec["version"], cmp
                        ),
                        "epss_score": epss_hit["score"] if epss_hit else 0.0,
                        "epss_percentile": (
                            epss_hit["percentile"] if epss_hit else 0.0
                        ),
                    }
                )
    findings.sort(
        key=lambda f: (
            f["ecosystem"],
            f["requested_package"],
            STATE_ORDER.get(f["state"], 9),
            f["cve"],
        )
    )
    return findings
