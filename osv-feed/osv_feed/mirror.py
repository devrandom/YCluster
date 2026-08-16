"""Mirror OSV ecosystem data into a local SQLite store without uploading host data.

Full ecosystem archives are downloaded once; afterwards only records listed
in the per-ecosystem `modified_id.csv` are fetched. All ecosystems live in a
single SQLite database so a snapshot is one self-contained file:

    <db_dir>/osv-feed.sqlite
    <db_dir>/<ecosystem>.cursor

The database uses the rollback journal (DELETE mode), so the `.sqlite` file
alone is always consistent once the writer closes — a copy of that single
file after a mirror completes is a valid snapshot. Redundant per-ecosystem
package indexes are not stored; matching filters the records that mention a
requested package via an in-database index in one pass.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
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


class _FeedConnection(sqlite3.Connection):
    """A connection that closes itself when used as a context manager.

    The stdlib ``with conn:`` commits/rolls back but leaves the connection
    open; closing here too means ``with connect(...) as conn:`` never leaks.
    """

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()
        return False


def db_path(db_dir: str) -> str:
    return os.path.join(db_dir, "osv-feed.sqlite")


def connect(
    db_dir: str,
    *,
    read_only: bool = False,
    immutable: bool = False,
) -> sqlite3.Connection:
    """Open the shared SQLite store.

    Read-only opens target a copy that is only ever replaced atomically
    (e.g. a snapshot on a read-only share); pass ``immutable=True`` there so
    SQLite performs no locking and never writes journal files to the share.
    """
    path = db_path(db_dir)
    if read_only:
        uri = f"file:{path}?mode=ro" + ("&immutable=1" if immutable else "")
        conn = sqlite3.connect(uri, uri=True, factory=_FeedConnection)
        conn.row_factory = sqlite3.Row
        return conn
    os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(path, factory=_FeedConnection)
    conn.row_factory = sqlite3.Row
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    # DELETE journaling keeps the database self-contained in one file so a
    # copy is always a valid snapshot, even from a plain file copy.
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS records(
          ecosystem TEXT NOT NULL,
          id TEXT NOT NULL,
          data TEXT NOT NULL,
          PRIMARY KEY (ecosystem, id)
        );
        CREATE TABLE IF NOT EXISTS packages(
          ecosystem TEXT NOT NULL,
          package TEXT NOT NULL,
          record_id TEXT NOT NULL,
          PRIMARY KEY (ecosystem, package, record_id)
        );
        CREATE INDEX IF NOT EXISTS idx_packages_package
          ON packages(ecosystem, package);
        """
    )


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
    if not _db_has_records(db_dir, ecosystem):
        return "absent"
    cursor = load_cursor(db_dir, ecosystem, now)
    if cursor == datetime.min.replace(tzinfo=now.tzinfo):
        return "stale"
    if cursor.tzinfo is None:
        cursor = cursor.replace(tzinfo=now.tzinfo)
    return "ok" if now - cursor <= max_age else "stale"


def _db_has_records(db_dir: str, ecosystem: str) -> bool:
    try:
        conn = connect(db_dir, read_only=True)
    except sqlite3.OperationalError:
        return False
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM records WHERE ecosystem=?", (ecosystem,)
        ).fetchone()
        return row[0] > 0
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def _record_packages(record: dict) -> set[str]:
    packages: set[str] = set()
    for entry in record.get("affected") or []:
        name = (entry.get("package") or {}).get("name")
        if name:
            packages.add(name.lower())
    return packages


def _insert_record(
    conn: sqlite3.Connection, ecosystem: str, record: dict
) -> None:
    record_id = record.get("id")
    conn.execute(
        "INSERT OR REPLACE INTO records(ecosystem, id, data) VALUES(?, ?, ?)",
        (ecosystem, record_id, json.dumps(record, ensure_ascii=False, sort_keys=True)),
    )
    conn.execute(
        "DELETE FROM packages WHERE ecosystem=? AND record_id=?",
        (ecosystem, record_id),
    )
    for package in _record_packages(record):
        conn.execute(
            "INSERT OR REPLACE INTO packages(ecosystem, package, record_id) "
            "VALUES(?, ?, ?)",
            (ecosystem, package, record_id),
        )


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

    _check_name(ecosystem)
    os.makedirs(db_dir, exist_ok=True)
    url = base_url.rstrip("/") + f"/{ecosystem}/all.zip"
    fd, tmp = tempfile.mkstemp(dir=db_dir, prefix="osv-all-", suffix=".zip")
    os.close(fd)
    report(f"{ecosystem}: downloading {url}")
    conn: sqlite3.Connection | None = None
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
        conn = connect(db_dir)
        count = 0
        # The full archive is authoritative for the ecosystem: replace all of
        # its rows atomically so a failed sync leaves the previous feed intact.
        with conn:
            conn.execute("DELETE FROM records WHERE ecosystem=?", (ecosystem,))
            conn.execute("DELETE FROM packages WHERE ecosystem=?", (ecosystem,))
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
                        raise MirrorError(
                            f"{ecosystem}/all.zip entry {name!r} has no id"
                        )
                    _insert_record(conn, ecosystem, record)
                    count += 1
        report(f"{ecosystem}: extracted {count} records")
    finally:
        if conn is not None:
            conn.close()
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass


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
    os.makedirs(db_dir, exist_ok=True)
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
    to_update: dict[str, dict] = {}
    to_remove: set[str] = set()
    for when, record_id in rows:
        if when <= cursor:
            break
        suffix = f"{ecosystem}/{record_id}.json"
        try:
            data = _http_get(base_url, suffix)
        except _NotFound:
            to_remove.add(record_id)
            report(f"{ecosystem}: removed {record_id} (withdrawn)")
            continue
        to_update[record_id] = json.loads(data.decode("utf-8"))
        report(f"{ecosystem}: updated {record_id}")

    if to_update or to_remove:
        conn = connect(db_dir)
        try:
            with conn:
                for record_id in to_remove:
                    conn.execute(
                        "DELETE FROM records WHERE ecosystem=? AND id=?",
                        (ecosystem, record_id),
                    )
                    conn.execute(
                        "DELETE FROM packages WHERE ecosystem=? AND record_id=?",
                        (ecosystem, record_id),
                    )
                for record_id, record in to_update.items():
                    _insert_record(conn, ecosystem, record)
        finally:
            conn.close()

    if rows:
        save_cursor(db_dir, ecosystem, newest)
    report(f"{ecosystem}: {len(to_update)} updated, {len(to_remove)} removed")
    return {"downloaded": len(to_update), "removed": len(to_remove), "unchanged": 0}
