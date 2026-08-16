import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from osv_feed import epss

_CSV = (
    "model_version,cve,epss,percentile,date\n"
    "v4.0.0,CVE-2024-0001,0.98765,0.99990,2026-08-15\n"
    "v4.0.0,CVE-2024-0002,0.00010,0.12340,2026-08-15\n"
)


class EpssModuleTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="osv-epss-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _write_file(self, name, content, binary=False):
        path = os.path.join(self.dir, name)
        mode = "wb" if binary else "w"
        with open(path, mode) as fh:
            fh.write(content)
        return path

    def test_download_plain_csv(self):
        path = self._write_file("epss.csv", _CSV)
        data = epss.download_epss(path)
        self.assertEqual(
            {"CVE-2024-0001": {"score": 0.98765, "percentile": 0.99990},
             "CVE-2024-0002": {"score": 0.00010, "percentile": 0.12340}},
            data,
        )

    def test_download_gzip_csv(self):
        gz = gzip.compress(_CSV.encode("utf-8"))
        path = self._write_file("epss.csv.gz", gz, binary=True)
        data = epss.download_epss(path)
        self.assertIn("CVE-2024-0001", data)

    def test_download_skips_malformed_rows(self):
        csv = (
            "cve,epss,percentile\n"
            "CVE-OK,0.5,0.9\n"
            "CVE-BAD,not-a-number,0.9\n"
            "CVE-EMPTY,,0.9\n"
        )
        path = self._write_file("bad.csv", csv)
        data = epss.download_epss(path)
        self.assertEqual({"CVE-OK": {"score": 0.5, "percentile": 0.9}}, data)

    def test_download_skips_model_version_comment_line(self):
        csv = (
            "#model_version:v2026.06.15,score_date:2026-08-16T12:03:43Z\n"
            "cve,epss,percentile\n"
            "CVE-1999-0001,0.03351,0.87656\n"
            "CVE-1999-0002,0.27858,0.9793\n"
        )
        path = self._write_file("live-format.csv", csv)
        data = epss.download_epss(path)
        self.assertEqual(
            {"CVE-1999-0001": {"score": 0.03351, "percentile": 0.87656},
             "CVE-1999-0002": {"score": 0.27858, "percentile": 0.9793}},
            data,
        )

    def test_download_skips_comment_line_after_gzip(self):
        gz = gzip.compress(
            ("#model_version:v1\n"
             "cve,epss,percentile\n"
             "CVE-GZ,0.1,0.2\n").encode("utf-8")
        )
        path = self._write_file("live-format.csv.gz", gz, binary=True)
        data = epss.download_epss(path)
        self.assertEqual({"CVE-GZ": {"score": 0.1, "percentile": 0.2}}, data)


    def test_save_and_load_roundtrip(self):
        data = {"CVE-1": {"score": 0.25, "percentile": 0.8}}
        epss.save_epss(self.dir, data)
        self.assertEqual(data, epss.load_epss(self.dir))

    def test_load_missing_returns_empty(self):
        self.assertEqual({}, epss.load_epss(self.dir))

    def test_status_absent(self):
        self.assertEqual("absent", epss.epss_status(self.dir))

    def test_status_ok(self):
        now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
        data = {"CVE-1": {"score": 0.1, "percentile": 0.2}}
        epss.save_epss(self.dir, data)
        with open(epss.epss_cursor_path(self.dir), "w") as fh:
            fh.write((now - timedelta(hours=1)).isoformat())
        self.assertEqual("ok", epss.epss_status(self.dir, now=now))

    def test_status_stale(self):
        now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
        data = {"CVE-1": {"score": 0.1, "percentile": 0.2}}
        epss.save_epss(self.dir, data)
        with open(epss.epss_cursor_path(self.dir), "w") as fh:
            fh.write((now - timedelta(days=3)).isoformat())
        self.assertEqual("stale", epss.epss_status(self.dir, now=now))

    def test_mirror_writes_snapshot_and_cursor(self):
        path = self._write_file("epss.csv", _CSV)
        now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
        count = epss.mirror_epss(self.dir, url=path, now=now)
        self.assertEqual(2, count)
        self.assertEqual(2, len(epss.load_epss(self.dir)))
        with open(epss.epss_cursor_path(self.dir)) as fh:
            self.assertEqual(now.isoformat(), fh.read().strip())

    def test_mirror_skips_when_fresh(self):
        path = self._write_file("epss.csv", _CSV)
        now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(2, epss.mirror_epss(self.dir, url=path, now=now))
        self.assertEqual(-1, epss.mirror_epss(self.dir, url=path, now=now))
        self.assertEqual(-1, epss.mirror_epss(self.dir, url=path, now=now + timedelta(hours=1)))

    def test_mirror_force_redownloads(self):
        path = self._write_file("epss.csv", _CSV)
        now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(2, epss.mirror_epss(self.dir, url=path, now=now))
        self.assertEqual(2, epss.mirror_epss(self.dir, url=path, now=now + timedelta(hours=1), force=True))


if __name__ == "__main__":
    unittest.main()
