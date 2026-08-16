"""Mirror and query the EPSS (Exploit Prediction Scoring System) feed.

EPSS scores each CVE with a probability that it is exploited in the wild
(0..1) plus a percentile rank. osv-feed mirrors the public daily snapshot
into the local DB directory, so matching can enrich every finding without
any outbound request at match time. Input may be the public URL or a local
file, keeping the tool usable in airgapped environments.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable

EPSS_URL = "https://epss.empiricalsecurity.com/epss_scores-current.csv.gz"

Progress = Callable[[str], None]


class EpssError(RuntimeError):
    pass


def epss_path(db_dir: str) -> str:
    return os.path.join(db_dir, "epss.json")


def epss_cursor_path(db_dir: str) -> str:
    return os.path.join(db_dir, "epss.cursor")


def load_epss(db_dir: str) -> dict[str, dict[str, float]]:
    """Return the mirrored EPSS map {cve: {score, percentile}} or {}."""
    try:
        with open(epss_path(db_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_epss(db_dir: str, data: dict[str, dict[str, float]]) -> None:
    os.makedirs(db_dir, exist_ok=True)
    path = epss_path(db_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, sort_keys=True, separators=(",", ":"))
    os.replace(tmp, path)


def _read_bytes(url: str) -> bytes:
    if url.startswith(("http://", "https://")):
        try:
            with urllib.request.urlopen(url, timeout=120) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            raise EpssError(f"GET {url}: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise EpssError(f"GET {url}: {exc.reason}") from exc
    with open(url, "rb") as fh:
        return fh.read()


def download_epss(url: str | None = None) -> dict[str, dict[str, float]]:
    """Download (or read locally) and parse the EPSS CSV.

    Returns {cve: {"score": float, "percentile": float}}. Accepts the gzipped
    official format and falls back to already-decompressed plain CSV.
    """
    raw = _read_bytes(url or EPSS_URL)
    try:
        content = gzip.decompress(raw).decode("utf-8")
    except (OSError, EOFError):
        content = raw.decode("utf-8", errors="replace")

    # The upstream snapshot may lead with a "#model_version:..." comment line
    # before the CSV header. DictReader takes the first non-comment line as the
    # column names, so drop comment lines up front to keep "cve/epss/percentile"
    # as the actual header.
    content = "\n".join(
        line for line in content.splitlines() if not line.lstrip().startswith("#")
    )

    data: dict[str, dict[str, float]] = {}
    reader = csv.DictReader(io.StringIO(content))
    for row in reader:
        cve = (row.get("cve") or "").strip()
        if not cve:
            continue
        try:
            data[cve] = {
                "score": float(row["epss"]),
                "percentile": float(row["percentile"]),
            }
        except (KeyError, ValueError):
            continue
    return data


def epss_status(
    db_dir: str,
    *,
    now: datetime | None = None,
    max_age: timedelta = timedelta(days=1),
) -> str:
    """Return ``ok``, ``absent``, or ``stale`` for the local EPSS snapshot."""
    now = now or datetime.now(timezone.utc)
    if not os.path.exists(epss_path(db_dir)):
        return "absent"
    try:
        with open(epss_cursor_path(db_dir), "r", encoding="utf-8") as fh:
            cursor = datetime.fromisoformat(fh.read().strip())
    except (FileNotFoundError, ValueError):
        return "stale"
    if cursor.tzinfo is None:
        cursor = cursor.replace(tzinfo=now.tzinfo)
    return "ok" if now - cursor <= max_age else "stale"


def mirror_epss(
    db_dir: str,
    *,
    force: bool = False,
    url: str | None = None,
    now: datetime | None = None,
    progress: Progress | None = None,
) -> int:
    """Mirror the EPSS snapshot into ``db_dir``.

    Skips the download when a fresh snapshot already exists (unless ``force``).
    Returns the number of CVEs written, or ``-1`` when nothing was downloaded.
    """
    now = now or datetime.now(timezone.utc)
    if not force and epss_status(db_dir, now=now) == "ok":
        return -1
    source = url or EPSS_URL
    if progress:
        progress(f"epss: downloading {source}")
    data = download_epss(url)
    save_epss(db_dir, data)
    cursor = epss_cursor_path(db_dir)
    tmp = cursor + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(now.astimezone(timezone.utc).isoformat())
    os.replace(tmp, cursor)
    return len(data)
