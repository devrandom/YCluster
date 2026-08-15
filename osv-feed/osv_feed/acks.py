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


def ack_key(category: str, ecosystem: str, cve: str) -> str:
    return "|".join((category, ecosystem, cve))


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
    note: str = "",
) -> dict:
    """Record an acknowledgment and return the stored entry.

    ``ecosystem`` may be empty to acknowledge the CVE across any ecosystem
    in the category (a wildcard); ``match`` checks the exact ecosystem first
    and falls back to the wildcard.
    """
    if not category or not cve:
        raise ValueError("category and cve are required")
    acks = load_acks(path)
    key = ack_key(category, ecosystem, cve)
    entry = {
        "category": category,
        "ecosystem": ecosystem,
        "cve": cve,
        "acked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "note": note,
    }
    acks[key] = entry
    _write_acks(path, acks)
    return entry


def remove_ack(path: str, *, category: str, ecosystem: str, cve: str) -> bool:
    """Remove an acknowledgment, returning True when one existed."""
    acks = load_acks(path)
    if ack_key(category, ecosystem, cve) not in acks:
        return False
    del acks[ack_key(category, ecosystem, cve)]
    _write_acks(path, acks)
    return True


def list_acks(path: str, category: str | None = None) -> list[dict]:
    entries = list(load_acks(path).values())
    if category is not None:
        entries = [entry for entry in entries if entry.get("category") == category]
    entries.sort(key=lambda entry: (entry.get("category", ""), entry.get("ecosystem", ""), entry.get("cve", "")))
    return entries
