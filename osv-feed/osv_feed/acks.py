"""Acknowledgment store for CVEs.

Acknowledged CVEs stay visible in match output so the audit trail is
preserved, but they are labeled so alerting rules can suppress them. The
store is a small JSON file whose location is independent of the feed mirror
(``[feed] ack_db`` / ``OSV_FEED_ACK_DB`` / ``--ack-db``), so it can live on
shared storage such as Ceph or a NAS.
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile


def ack_key(category: str, ecosystem: str, cve: str, surface: str = "") -> str:
    """Return the ack store key.

    ``surface`` scopes the ack to one deployment surface (e.g. a container
    image name, or a host/service install target like ``local-ai-proxy``).
    An empty ``surface`` is a wildcard and keeps the legacy three-part key, so
    existing surface-less acks stay valid.
    """
    if surface:
        return "|".join((category, ecosystem, cve, surface))
    return "|".join((category, ecosystem, cve))


def find_ack(
    acks: dict[str, dict],
    category: str,
    ecosystem: str,
    cve: str,
    surface: str = "",
) -> dict | None:
    """Return the most specific ack matching a finding, or None.

    Precedence is most-specific-first: exact ecosystem + exact surface, then
    exact ecosystem + wildcard surface, then wildcard ecosystem + exact
    surface, then wildcard ecosystem + wildcard surface. An empty ``surface``
    is the wildcard tier; a finding with no surface matches only wildcard
    acks, and a surface-scoped ack matches only findings on that surface. An
    empty ``ecosystem`` is likewise a wildcard across categories. Legacy
    surface-less acks keep the three-part key and are found through the
    wildcard-surface tiers, so existing acks stay valid.
    """
    candidates = (
        ack_key(category, ecosystem, cve, surface),
        ack_key(category, ecosystem, cve, ""),
        ack_key(category, "", cve, surface),
        ack_key(category, "", cve, ""),
    )
    for key in candidates:
        ack = acks.get(key)
        if ack is not None:
            return ack
    return None


def load_acks(path: str) -> dict[str, dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise ValueError(f"invalid ack DB {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"invalid ack DB {path}: expected a JSON object")
    return {str(key): value for key, value in data.items() if isinstance(value, dict)}


def _write_acks(path: str, acks: dict[str, dict]) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".acks-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(acks, fh, ensure_ascii=False, sort_keys=True, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        raise


def add_ack(
    path: str,
    *,
    category: str,
    ecosystem: str,
    cve: str,
    surface: str = "",
    note: str = "",
) -> dict:
    """Record an acknowledgment and return the stored entry.

    ``ecosystem`` may be empty to acknowledge the CVE across any ecosystem
    in the category (a wildcard); ``match`` checks the exact ecosystem first
    and falls back to the wildcard. ``surface`` scopes the ack to one
    deployment surface (e.g. an image); empty means any surface (wildcard).
    """
    if not category or not cve:
        raise ValueError("category and cve are required")
    acks = load_acks(path)
    key = ack_key(category, ecosystem, cve, surface)
    entry = {
        "category": category,
        "ecosystem": ecosystem,
        "cve": cve,
        "surface": surface,
        "acked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "note": note,
    }
    acks[key] = entry
    _write_acks(path, acks)
    return entry


def remove_ack(
    path: str, *, category: str, ecosystem: str, cve: str, surface: str = ""
) -> bool:
    """Remove an acknowledgment, returning True when one existed."""
    acks = load_acks(path)
    if ack_key(category, ecosystem, cve, surface) not in acks:
        return False
    del acks[ack_key(category, ecosystem, cve, surface)]
    _write_acks(path, acks)
    return True


def list_acks(
    path: str, category: str | None = None, surface: str | None = None
) -> list[dict]:
    entries = list(load_acks(path).values())
    if category is not None:
        entries = [entry for entry in entries if entry.get("category") == category]
    if surface is not None:
        entries = [entry for entry in entries if (entry.get("surface") or "") == surface]
    entries.sort(
        key=lambda entry: (
            entry.get("category", ""),
            entry.get("ecosystem", ""),
            entry.get("cve", ""),
            entry.get("surface", ""),
        )
    )
    return entries
