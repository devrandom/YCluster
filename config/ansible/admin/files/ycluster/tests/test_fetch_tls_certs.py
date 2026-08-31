import tempfile
from unittest import mock
import unittest
from pathlib import Path

from ycluster.utils import fetch_tls_certs


class FetchTlsCertsTests(unittest.TestCase):
    def test_read_if_changed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'cert.pem'
            path.write_text('old')
            self.assertFalse(fetch_tls_certs._read_if_changed(str(path), 'old'))
            self.assertTrue(fetch_tls_certs._read_if_changed(str(path), 'new'))
            self.assertTrue(fetch_tls_certs._read_if_changed(Path(td) / 'missing', 'new'))

    def test_write_updated_files_reports_no_change(self):
        with tempfile.TemporaryDirectory() as td:
            cert = Path(td) / 'cert.pem'
            key = Path(td) / 'key.pem'
            with mock.patch.object(fetch_tls_certs, '_read_if_changed', side_effect=[False, False]):
                self.assertFalse(fetch_tls_certs._write_updated_files('c', 'k'))
            self.assertFalse(cert.exists())
            self.assertFalse(key.exists())


if __name__ == '__main__':
    unittest.main()
