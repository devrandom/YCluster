import functools
import http.server
import json
import socket
from unittest import mock
import os
import shutil
import tempfile
import threading
import unittest
import zipfile
from datetime import datetime, timedelta, timezone

import urllib.error

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


    def test_incremental_sync_resumes_after_partial_failure(self):
        # A run that dies after applying some timestamp groups must not
        # discard the earlier groups: the next run resumes below the last
        # committed cursor and only fetches what is still outstanding.
        _build_bucket(self.root, "Ubuntu", [_record("UBUNTU-CVE-A")])
        mirror.mirror_ecosystem(self.db, "Ubuntu", base_url=self.server.base_url)
        _build_bucket(
            self.root,
            "Ubuntu",
            [
                _record("UBUNTU-CVE-D"),
                _record("UBUNTU-CVE-C"),
                _record("UBUNTU-CVE-B"),
            ],
            ts_order=[
                "2026-08-15T04:00:00Z",
                "2026-08-15T03:00:00Z",
                "2026-08-15T02:00:00Z",
            ],
        )
        real = mirror._http_get

        def flaky(base_url, suffix, progress=None):
            if suffix == "Ubuntu/UBUNTU-CVE-D.json":
                raise mirror.MirrorError("simulated network failure")
            return real(base_url, suffix, progress=progress)

        with mock.patch("osv_feed.mirror._http_get", side_effect=flaky):
            with self.assertRaises(mirror.MirrorError):
                mirror.mirror_ecosystem(
                    self.db, "Ubuntu", base_url=self.server.base_url
                )
        # Oldest groups B and C were committed before D failed; cursor sits at C.
        self.assertEqual(
            {"UBUNTU-CVE-A", "UBUNTU-CVE-B", "UBUNTU-CVE-C"},
            _record_ids(self.db, "Ubuntu"),
        )
        cursor = mirror.load_cursor(self.db, "Ubuntu", datetime(2026, 8, 15, tzinfo=timezone.utc))
        self.assertEqual(datetime(2026, 8, 15, 3, 0, tzinfo=timezone.utc), cursor)

        # Resume fetches only the outstanding D record, no re-application.
        result = mirror.mirror_ecosystem(
            self.db, "Ubuntu", base_url=self.server.base_url
        )
        self.assertEqual(result["downloaded"], 1)
        self.assertEqual(
            {"UBUNTU-CVE-A", "UBUNTU-CVE-B", "UBUNTU-CVE-C", "UBUNTU-CVE-D"},
            _record_ids(self.db, "Ubuntu"),
        )

    def test_deep_backlog_uses_archive_sync(self):
        # A large pending backlog must rebuild from the bulk all.zip archive
        # rather than replay thousands of per-record fetches (+ thousands of
        # tiny commits/fsyncs) on the collector node.
        _build_bucket(self.root, "Ubuntu", [_record("UBUNTU-CVE-A")])
        mirror.mirror_ecosystem(self.db, "Ubuntu", base_url=self.server.base_url)
        _build_bucket(
            self.root,
            "Ubuntu",
            [
                _record("UBUNTU-CVE-B"),
                _record("UBUNTU-CVE-C"),
                _record("UBUNTU-CVE-D"),
            ],
            ts_order=[
                "2026-08-15T04:00:00Z",
                "2026-08-15T03:00:00Z",
                "2026-08-15T02:00:00Z",
            ],
        )
        archive_calls = []
        real_archive = mirror.download_all_zip
        real_get = mirror._http_get

        def spy_archive(*args, **kwargs):
            archive_calls.append(args)
            return real_archive(*args, **kwargs)

        def no_per_record(base_url, suffix, progress=None):
            # modified_id.csv is read (once) to compute the pending count before
            # the archive fallback kicks in; per-record .json fetches must not
            # happen.
            if suffix == "Ubuntu/modified_id.csv":
                return real_get(base_url, suffix, progress=progress)
            raise AssertionError("deep backlog must not replay per-record fetches")

        with mock.patch.object(mirror, "_FULL_SYNC_THRESHOLD", 2), mock.patch.object(
            mirror, "download_all_zip", side_effect=spy_archive
        ), mock.patch.object(mirror, "_http_get", side_effect=no_per_record):
            result = mirror.mirror_ecosystem(
                self.db, "Ubuntu", base_url=self.server.base_url
            )
        self.assertTrue(archive_calls, "expected a full archive sync for the backlog")
        self.assertEqual(result["downloaded"], 3)
        self.assertEqual(
            {"UBUNTU-CVE-B", "UBUNTU-CVE-C", "UBUNTU-CVE-D"},
            _record_ids(self.db, "Ubuntu"),
        )

    def test_http_get_retries_transient_timeout(self):
        class _FakeResp:
            def __init__(self, data):
                self._data = data

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return self._data

        calls = {"n": 0}

        def flaky(url, timeout=None):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise urllib.error.URLError(socket.timeout("read timeout"))
            return _FakeResp(b"{}")

        progress = []
        with mock.patch("osv_feed.mirror.urllib.request.urlopen", side_effect=flaky), mock.patch(
            "osv_feed.mirror.time.sleep"
        ):
            data = mirror._http_get(
                self.server.base_url, "Ubuntu/UBUNTU-CVE-A.json", progress=progress.append
            )
        self.assertEqual(b"{}", data)
        self.assertGreaterEqual(calls["n"], 3)
        self.assertTrue(any("retrying" in line for line in progress))


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
