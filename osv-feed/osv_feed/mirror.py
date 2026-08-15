"""Mirror OSV ecosystem data to a local directory without uploading host data.

Full ecosystem archives are downloaded once; afterwards only records listed
in the per-ecosystem `modified_id.csv` are fetched. The directory layout is
intentionally filesystem-native so it can be scanned by osv-scanner:

    <db_dir>/<ecosystem>/<id>.json
    <db_dir>/<ecosystem>.cursor
"""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Callable

GCS_BASE = "https://osv-vulnerabilities.storage.googleapis.com"

Progress = Callable[[str], None]


class MirrorError(RuntimeError):
    pass


def ecosystem_dir(db_dir: str, ecosystem: str) -> str:
    _check_name(ecosystem)
    return os.path.join(db_dir, ecosystem)


def record_path(db_dir: str, ecosystem: str, record_id: str) -> str:
    _check_name(record_id)
    return os.path.join(ecosystem_dir(db_dir, ecosystem), record_id + ".json")


def cursor_path(db_dir: str, ecosystem: str) -> str:
    return os.path.join(db_dir, ecosystem + ".cursor")


def load_cursor(db_dir: str, ecosystem: str, now: datetime) -> datetime:
    path = cursor_path(db_dir, ecosystem)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return datetime.fromisoformat(fh.read().strip())
    except (FileNotFoundError, ValueError):
        return datetime.min.replace(tzinfo=now.tzinfo)


def save_cursor(db_dir: str, ecosystem: str, value: datetime) -> None:
    tmp = cursor_path(db_dir, ecosystem) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(value.astimezone(timezone.utc).isoformat())
    os.replace(tmp, cursor_path(db_dir, ecosystem))


def feed_status(
    db_dir: str,
    ecosystem: str,
    *,
    now: datetime | None = None,
    max_age: timedelta = timedelta(days=1),
) -> str:
    """Return ``ok``, ``absent``, or ``stale`` for a local ecosystem feed.

    ``absent`` means no mirrored records exist. ``stale`` means records exist
    but there is no usable cursor or the last sync is older than ``max_age``.
    """
    now = now or datetime.now(timezone.utc)
    directory = ecosystem_dir(db_dir, ecosystem)
    if not os.path.isdir(directory) or not any(
        name.endswith(".json") for name in os.listdir(directory)
    ):
        return "absent"
    cursor = load_cursor(db_dir, ecosystem, now)
    if cursor == datetime.min.replace(tzinfo=now.tzinfo):
        return "stale"
    if cursor.tzinfo is None:
        cursor = cursor.replace(tzinfo=now.tzinfo)
    return "ok" if now - cursor <= max_age else "stale"


INDEX_VERSION = 1


def index_path(db_dir: str, ecosystem: str) -> str:
    _check_name(ecosystem)
    return os.path.join(db_dir, ecosystem + ".index.json")


def _empty_index() -> dict:
    return {"version": INDEX_VERSION, "packages": {}, "records": {}}


def _record_packages(record: dict) -> set[str]:
    packages: set[str] = set()
    for entry in record.get("affected") or []:
        name = (entry.get("package") or {}).get("name")
        if name:
            packages.add(name.lower())
    return packages


def _index_add_record(index: dict, record_id: str, record: dict) -> None:
    if not record_id:
        return
    previous = index["records"].get(record_id, [])
    for package in previous:
        ids = index["packages"].get(package, [])
        if record_id in ids:
            ids.remove(record_id)
        if not ids:
            index["packages"].pop(package, None)
    packages = sorted(_record_packages(record))
    index["records"][record_id] = packages
    for package in packages:
        if record_id not in index["packages"].setdefault(package, []):
            index["packages"][package].append(record_id)


def _index_remove_record(index: dict, record_id: str) -> None:
    previous = index["records"].pop(record_id, [])
    for package in previous:
        ids = index["packages"].get(package, [])
        if record_id in ids:
            ids.remove(record_id)
        if not ids:
            index["packages"].pop(package, None)


def load_index(db_dir: str, ecosystem: str) -> dict | None:
    path = index_path(db_dir, ecosystem)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError):
        return None
    if data.get("version") != INDEX_VERSION:
        return None
    data.setdefault("packages", {})
    data.setdefault("records", {})
    return data


def save_index(db_dir: str, ecosystem: str, index: dict) -> None:
    path = index_path(db_dir, ecosystem)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(index, fh, sort_keys=True, separators=(",", ":"))
    os.replace(tmp, path)


def build_index(db_dir: str, ecosystem: str) -> dict:
    """Build an in-memory package-to-record index from local records."""
    index = _empty_index()
    directory = ecosystem_dir(db_dir, ecosystem)
    if os.path.isdir(directory):
        for name in os.listdir(directory):
            if not name.endswith(".json"):
                continue
            path = os.path.join(directory, name)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    record = json.load(fh)
            except (OSError, ValueError) as exc:
                raise ValueError(f"cannot read OSV record {path}: {exc}") from exc
            _index_add_record(index, name[:-5], record)
    return index


def rebuild_index(db_dir: str, ecosystem: str) -> dict:
    index = build_index(db_dir, ecosystem)
    save_index(db_dir, ecosystem, index)
    return index


def _check_name(value: str) -> None:
    if not value or "/" in value or "\\" in value or ".." in value:
        raise MirrorError(f"unsafe OSV name: {value!r}")


def _http_get(base_url: str, suffix: str) -> bytes:
    url = base_url.rstrip("/") + "/" + suffix
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise _NotFound(url) from exc
        raise MirrorError(f"GET {url}: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise MirrorError(f"GET {url}: {exc.reason}") from exc


class _NotFound(RuntimeError):
    pass


def download_all_zip(
    db_dir: str,
    ecosystem: str,
    base_url: str = GCS_BASE,
    progress: Progress | None = None,
) -> None:
    def report(message: str) -> None:
        if progress:
            progress(message)

    dest = ecosystem_dir(db_dir, ecosystem)
    os.makedirs(dest, exist_ok=True)
    url = base_url.rstrip("/") + f"/{ecosystem}/all.zip"
    fd, tmp = tempfile.mkstemp(dir=db_dir, prefix="osv-all-", suffix=".zip")
    os.close(fd)
    report(f"{ecosystem}: downloading {url}")
    try:
        try:
            with urllib.request.urlopen(url, timeout=120) as resp:
                size = 0
                with open(tmp, "wb") as out:
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        size += len(chunk)
        except urllib.error.HTTPError as exc:
            raise MirrorError(f"GET {url}: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise MirrorError(f"GET {url}: {exc.reason}") from exc
        report(f"{ecosystem}: downloaded {size} bytes, extracting")
        index = _empty_index()
        with zipfile.ZipFile(tmp) as zf:
            names = [
                name
                for name in zf.namelist()
                if name.lower().endswith(".json") and not name.endswith("/")
            ]
            for name in names:
                record = json.loads(zf.read(name).decode("utf-8"))
                record_id = record.get("id")
                if not record_id:
                    raise MirrorError(f"{ecosystem}/all.zip entry {name!r} has no id")
                _write_record(db_dir, ecosystem, record_id, record)
                _index_add_record(index, record_id, record)
        save_index(db_dir, ecosystem, index)
        report(f"{ecosystem}: extracted {len(names)} records")
    finally:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass


def _write_record(db_dir: str, ecosystem: str, record_id: str, record: dict) -> None:
    _check_name(record_id)
    path = record_path(db_dir, ecosystem, record_id)
    fd, tmp = tempfile.mkstemp(dir=ecosystem_dir(db_dir, ecosystem), prefix=".osv-", suffix=".json")
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        raise


def _read_modified_rows(base_url: str, ecosystem: str) -> list[tuple[datetime, str]]:
    suffix = f"{ecosystem}/modified_id.csv"
    text = _http_get(base_url, suffix).decode("utf-8")
    rows: list[tuple[datetime, str]] = []
    for line in csv.reader(io.StringIO(text)):
        if not line or not line[0].strip():
            continue
        try:
            when = datetime.fromisoformat(line[0].strip())
        except ValueError:
            continue
        if len(line) < 2 or not line[1].strip():
            continue
        rows.append((when, line[1].strip()))
    rows.sort(reverse=True)
    return rows


def mirror_ecosystem(
    db_dir: str,
    ecosystem: str,
    *,
    force_full: bool = False,
    base_url: str = GCS_BASE,
    now: datetime | None = None,
    progress: Progress | None = None,
) -> dict[str, int]:
    """Sync one ecosystem; returns counts for (downloaded, removed, unchanged)."""
    def report(message: str) -> None:
        if progress:
            progress(message)

    _check_name(ecosystem)
    now = now or datetime.now(timezone.utc)
    os.makedirs(ecosystem_dir(db_dir, ecosystem), exist_ok=True)
    cursor = load_cursor(db_dir, ecosystem, now)
    rows = _read_modified_rows(base_url, ecosystem)
    newest = rows[0][0] if rows else cursor

    if cursor == datetime.min.replace(tzinfo=now.tzinfo) or force_full:
        report(f"{ecosystem}: full archive sync")
        download_all_zip(db_dir, ecosystem, base_url=base_url, progress=progress)
        if rows:
            save_cursor(db_dir, ecosystem, newest)
        return {"downloaded": len(rows), "removed": 0, "unchanged": 0}

    report(f"{ecosystem}: incremental sync since {cursor.isoformat()}")
    index = load_index(db_dir, ecosystem)
    index_dirty = False
    downloaded = 0
    removed = 0
    for when, record_id in rows:
        if when <= cursor:
            break
        suffix = f"{ecosystem}/{record_id}.json"
        try:
            data = _http_get(base_url, suffix)
        except _NotFound:
            try:
                os.remove(record_path(db_dir, ecosystem, record_id))
            except FileNotFoundError:
                pass
            if index is not None:
                _index_remove_record(index, record_id)
                index_dirty = True
            removed += 1
            report(f"{ecosystem}: removed {record_id} (withdrawn)")
            continue
        record = json.loads(data.decode("utf-8"))
        _write_record(db_dir, ecosystem, record_id, record)
        if index is not None:
            _index_add_record(index, record_id, record)
            index_dirty = True
        downloaded += 1
        report(f"{ecosystem}: updated {record_id}")
    if rows:
        save_cursor(db_dir, ecosystem, newest)
    if index is None:
        rebuild_index(db_dir, ecosystem)
    elif index_dirty:
        save_index(db_dir, ecosystem, index)
    report(f"{ecosystem}: {downloaded} updated, {removed} removed")
    return {"downloaded": downloaded, "removed": removed, "unchanged": 0}
