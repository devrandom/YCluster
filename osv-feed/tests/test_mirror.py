import functools
import http.server
import json
import os
import shutil
import tempfile
import threading
import unittest
import zipfile
from datetime import datetime, timedelta, timezone

from osv_feed import mirror


class _BucketHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class _BucketServer:
    def __init__(self, root):
        self.root = root
        handler = functools.partial(_BucketHandler, directory=root)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        host, port = self.server.server_address
        return f"http://127.0.0.1:{port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _record(record_id, package="openssh", fixed=None):
    events = [{"introduced": "0"}]
    if fixed:
        events.append({"fixed": fixed})
    return {
        "id": record_id,
        "summary": f"test {record_id}",
        "affected": [
            {
                "package": {"ecosystem": "Ubuntu", "name": package},
                "ranges": [{"type": "ECOSYSTEM", "events": events}],
            }
        ],
    }


def _build_bucket(root, ecosystem, records, ts_order=None):
    directory = os.path.join(root, ecosystem)
    os.makedirs(directory, exist_ok=True)
    zip_path = os.path.join(directory, "all.zip")
    with zipfile.ZipFile(zip_path, "w") as zf:
        for record in records:
            zf.writestr(f"{ecosystem}/{record['id']}.json", json.dumps(record))
            with open(
                os.path.join(directory, record["id"] + ".json"), "w", encoding="utf-8"
            ) as fh:
                json.dump(record, fh)
    ts_order = ts_order or [
        f"2026-08-15T{index:02d}:00:00Z" for index in range(len(records), 0, -1)
    ]
    rows = "\n".join(f"{when},{record['id']}" for when, record in zip(ts_order, records))
    with open(os.path.join(directory, "modified_id.csv"), "w", encoding="utf-8") as fh:
        fh.write(rows + "\n")


def _record_ids(db, ecosystem, conn=None):
    if conn is None:
        with mirror.connect(db, read_only=True) as conn:
            return _record_ids(db, ecosystem, conn=conn)
    return {
        row["id"]
        for row in conn.execute(
            "SELECT id FROM records WHERE ecosystem=?", (ecosystem,)
        )
    }


class MirrorTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="osv-bucket-")
        self.db = tempfile.mkdtemp(prefix="osv-db-")
        self.server = _BucketServer(self.root)
        self.addCleanup(self.server.close)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.db, ignore_errors=True)

    def test_initial_full_download(self):
        _build_bucket(
            self.root,
            "Ubuntu",
            [_record("UBUNTU-CVE-1"), _record("UBUNTU-CVE-2")],
        )
        progress = []
        result = mirror.mirror_ecosystem(
            self.db,
            "Ubuntu",
            base_url=self.server.base_url,
            progress=progress.append,
        )
        self.assertTrue(os.path.exists(mirror.db_path(self.db)))
        self.assertEqual(
            {"UBUNTU-CVE-1", "UBUNTU-CVE-2"}, _record_ids(self.db, "Ubuntu")
        )
        self.assertTrue(os.path.exists(mirror.cursor_path(self.db, "Ubuntu")))
        with mirror.connect(self.db, read_only=True) as conn:
            packages = {
                row["package"]
                for row in conn.execute(
                    "SELECT package FROM packages WHERE ecosystem='Ubuntu'"
                )
            }
        self.assertEqual({"openssh"}, packages)
        self.assertEqual(result["downloaded"], 2)
        joined = "\n".join(progress)
        self.assertIn("downloading", joined)
        self.assertIn("downloaded ", joined)
        self.assertIn("extracted 2 records", joined)

    def test_incremental_only_fetches_changed_records(self):
        _build_bucket(self.root, "Ubuntu", [_record("UBUNTU-CVE-A")])
        mirror.mirror_ecosystem(self.db, "Ubuntu", base_url=self.server.base_url)
        _build_bucket(
            self.root,
            "Ubuntu",
            [_record("UBUNTU-CVE-B"), _record("UBUNTU-CVE-A")],
            ts_order=["2026-08-15T02:00:00Z", "2026-08-15T01:00:00Z"],
        )
        progress = []
        result = mirror.mirror_ecosystem(
            self.db,
            "Ubuntu",
            base_url=self.server.base_url,
            progress=progress.append,
        )
        self.assertEqual(result["downloaded"], 1)
        self.assertEqual({"UBUNTU-CVE-A", "UBUNTU-CVE-B"}, _record_ids(self.db, "Ubuntu"))
        joined = "\n".join(progress)
        self.assertIn("updated UBUNTU-CVE-B", joined)
        self.assertNotIn("updated UBUNTU-CVE-A", joined)
        self.assertIn("1 updated, 0 removed", joined)

    def test_withdrawn_record_is_removed(self):
        _build_bucket(self.root, "Ubuntu", [_record("UBUNTU-CVE-A")])
        mirror.mirror_ecosystem(self.db, "Ubuntu", base_url=self.server.base_url)
        self.assertEqual({"UBUNTU-CVE-A"}, _record_ids(self.db, "Ubuntu"))
        os.remove(os.path.join(self.root, "Ubuntu", "UBUNTU-CVE-A.json"))
        os.remove(os.path.join(self.root, "Ubuntu", "all.zip"))
        _build_bucket(self.root, "Ubuntu", [], ts_order=[])
        rows = "2026-08-15T02:00:00Z,UBUNTU-CVE-A\n"
        with open(
            os.path.join(self.root, "Ubuntu", "modified_id.csv"), "w", encoding="utf-8"
        ) as fh:
            fh.write(rows)
        progress = []
        result = mirror.mirror_ecosystem(
            self.db,
            "Ubuntu",
            base_url=self.server.base_url,
            progress=progress.append,
        )
        self.assertEqual(result["removed"], 1)
        self.assertEqual(set(), _record_ids(self.db, "Ubuntu"))
        with mirror.connect(self.db, read_only=True) as conn:
            packages = {
                row["package"]
                for row in conn.execute(
                    "SELECT package FROM packages WHERE ecosystem='Ubuntu'"
                )
            }
        self.assertEqual(set(), packages)
        self.assertIn("removed UBUNTU-CVE-A (withdrawn)", "\n".join(progress))


class FeedStatusTest(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.mkdtemp(prefix="osv-status-")
        self.addCleanup(shutil.rmtree, self.db, ignore_errors=True)
        self.now = datetime(2026, 8, 15, tzinfo=timezone.utc)

    def _write_record(self):
        with mirror.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO records(ecosystem, id, data) "
                "VALUES('Ubuntu', 'UBUNTU-CVE-1', ?)",
                (json.dumps({"id": "UBUNTU-CVE-1"}),),
            )

    def test_absent_when_database_missing(self):
        self.assertEqual("absent", mirror.feed_status(self.db, "Ubuntu", now=self.now))

    def test_absent_when_no_records_for_ecosystem(self):
        mirror.connect(self.db).close()
        self.assertEqual("absent", mirror.feed_status(self.db, "Ubuntu", now=self.now))
        # A different ecosystem's records do not imply this one is present.
        with mirror.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO records(ecosystem, id, data) "
                "VALUES('Debian', 'DEBIAN-CVE-1', ?)",
                (json.dumps({"id": "DEBIAN-CVE-1"}),),
            )
        self.assertEqual("absent", mirror.feed_status(self.db, "Ubuntu", now=self.now))

    def test_ok_when_fresh_cursor(self):
        self._write_record()
        mirror.save_cursor(self.db, "Ubuntu", self.now)
        self.assertEqual("ok", mirror.feed_status(self.db, "Ubuntu", now=self.now))

    def test_stale_when_cursor_old(self):
        self._write_record()
        mirror.save_cursor(self.db, "Ubuntu", self.now - timedelta(days=2))
        self.assertEqual("stale", mirror.feed_status(self.db, "Ubuntu", now=self.now))

    def test_stale_when_cursor_missing(self):
        self._write_record()
        self.assertEqual("stale", mirror.feed_status(self.db, "Ubuntu", now=self.now))

    def test_insert_record_replaces_package_rows(self):
        with mirror.connect(self.db) as conn:
            mirror._insert_record(
                conn,
                "Ubuntu",
                {"id": "UBUNTU-CVE-R", "affected": [{"package": {"name": "openssh"}}]},
            )
            mirror._insert_record(
                conn,
                "Ubuntu",
                {
                    "id": "UBUNTU-CVE-R",
                    "affected": [
                        {"package": {"name": "openssh"}},
                        {"package": {"name": "openssl"}},
                    ],
                },
            )
        with mirror.connect(self.db, read_only=True) as conn:
            packages = {
                row["package"]
                for row in conn.execute(
                    "SELECT package FROM packages "
                    "WHERE ecosystem='Ubuntu' AND record_id='UBUNTU-CVE-R'"
                )
            }
        self.assertEqual({"openssh", "openssl"}, packages)

    def test_boundary_age_is_fresh(self):
        self._write_record()
        mirror.save_cursor(self.db, "Ubuntu", self.now - timedelta(days=1))
        self.assertEqual("ok", mirror.feed_status(self.db, "Ubuntu", now=self.now))


if __name__ == "__main__":
    unittest.main()
