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
            with open(os.path.join(directory, record["id"] + ".json"), "w", encoding="utf-8") as fh:
                json.dump(record, fh)
    ts_order = ts_order or [
        f"2026-08-15T{index:02d}:00:00Z" for index in range(len(records), 0, -1)
    ]
    rows = "\n".join(f"{when},{record['id']}" for when, record in zip(ts_order, records))
    with open(os.path.join(directory, "modified_id.csv"), "w", encoding="utf-8") as fh:
        fh.write(rows + "\n")


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
        self.assertTrue(os.path.exists(mirror.record_path(self.db, "Ubuntu", "UBUNTU-CVE-1")))
        self.assertTrue(os.path.exists(mirror.record_path(self.db, "Ubuntu", "UBUNTU-CVE-2")))
        self.assertTrue(os.path.exists(mirror.cursor_path(self.db, "Ubuntu")))
        index = mirror.load_index(self.db, "Ubuntu")
        self.assertIsNotNone(index)
        self.assertIn("UBUNTU-CVE-1", index["packages"]["openssh"])
        self.assertIn("UBUNTU-CVE-2", index["packages"]["openssh"])
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
        self.assertTrue(os.path.exists(mirror.record_path(self.db, "Ubuntu", "UBUNTU-CVE-A")))
        self.assertTrue(os.path.exists(mirror.record_path(self.db, "Ubuntu", "UBUNTU-CVE-B")))
        joined = "\n".join(progress)
        self.assertIn("updated UBUNTU-CVE-B", joined)
        self.assertNotIn("updated UBUNTU-CVE-A", joined)
        self.assertIn("1 updated, 0 removed", joined)
        index = mirror.load_index(self.db, "Ubuntu")
        self.assertIn("UBUNTU-CVE-A", index["packages"]["openssh"])
        self.assertIn("UBUNTU-CVE-B", index["packages"]["openssh"])

    def test_withdrawn_record_is_removed(self):
        _build_bucket(self.root, "Ubuntu", [_record("UBUNTU-CVE-A")])
        mirror.mirror_ecosystem(self.db, "Ubuntu", base_url=self.server.base_url)
        record_path = mirror.record_path(self.db, "Ubuntu", "UBUNTU-CVE-A")
        self.assertTrue(os.path.exists(record_path))
        os.remove(os.path.join(self.root, "Ubuntu", "UBUNTU-CVE-A.json"))
        os.remove(os.path.join(self.root, "Ubuntu", "all.zip"))
        _build_bucket(
            self.root,
            "Ubuntu",
            [],
            ts_order=[],
        )
        rows = "2026-08-15T02:00:00Z,UBUNTU-CVE-A\n"
        with open(os.path.join(self.root, "Ubuntu", "modified_id.csv"), "w", encoding="utf-8") as fh:
            fh.write(rows)
        # The server replaces the csv; also ensure there is no record file served.
        progress = []
        result = mirror.mirror_ecosystem(
            self.db,
            "Ubuntu",
            base_url=self.server.base_url,
            progress=progress.append,
        )
        self.assertEqual(result["removed"], 1)
        self.assertFalse(os.path.exists(record_path))
        self.assertIn("removed UBUNTU-CVE-A (withdrawn)", "\n".join(progress))
        index = mirror.load_index(self.db, "Ubuntu")
        self.assertNotIn("UBUNTU-CVE-A", index["records"])
        self.assertNotIn("openssh", index["packages"])


class FeedStatusTest(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.mkdtemp(prefix="osv-status-")
        self.addCleanup(shutil.rmtree, self.db, ignore_errors=True)
        self.now = datetime(2026, 8, 15, tzinfo=timezone.utc)

    def _write_record(self):
        os.makedirs(mirror.ecosystem_dir(self.db, "Ubuntu"), exist_ok=True)
        path = mirror.record_path(self.db, "Ubuntu", "UBUNTU-CVE-1")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"id": "UBUNTU-CVE-1"}, fh)

    def test_absent_when_directory_missing(self):
        self.assertEqual("absent", mirror.feed_status(self.db, "Ubuntu", now=self.now))

    def test_absent_when_directory_empty(self):
        os.makedirs(mirror.ecosystem_dir(self.db, "Ubuntu"), exist_ok=True)
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

    def test_index_add_record_replaces_previous_packages(self):
        index = mirror._empty_index()
        mirror._index_add_record(
            index,
            "UBUNTU-CVE-R",
            {"affected": [{"package": {"name": "openssh"}}]},
        )
        mirror._index_add_record(
            index,
            "UBUNTU-CVE-R",
            {
                "affected": [
                    {"package": {"name": "openssh"}},
                    {"package": {"name": "openssl"}},
                ]
            },
        )
        self.assertEqual(["openssh", "openssl"], index["records"]["UBUNTU-CVE-R"])
        self.assertIn("UBUNTU-CVE-R", index["packages"]["openssl"])

    def test_boundary_age_is_fresh(self):
        self._write_record()
        mirror.save_cursor(self.db, "Ubuntu", self.now - timedelta(days=1))
        self.assertEqual("ok", mirror.feed_status(self.db, "Ubuntu", now=self.now))


if __name__ == "__main__":
    unittest.main()
