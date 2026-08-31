"""Tests for certbot status TLS-material rendering."""

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from ycluster.cli import certbot


class TLSMaterialsTests(unittest.TestCase):
    @staticmethod
    def _render(fake_client):
        out = io.StringIO()
        with mock.patch.object(certbot, 'get_etcd_client', return_value=fake_client):
            with redirect_stdout(out):
                certbot._print_tls_materials_status()
        return out.getvalue()

    def test_both_present_with_nginx(self):
        client = mock.Mock()
        client.get = mock.Mock(side_effect=[
            (b'key', None), (b'cert', None)])
        with mock.patch.object(certbot, 'Path') as path:
            path.return_value = path
            path('/etc/nginx/ssl/key.pem').exists.return_value = True
            path('/etc/nginx/ssl/cert.pem').exists.return_value = True
            text = self._render(client)
        self.assertIn('TLS materials: Present in etcd', text)
        self.assertIn('Nginx TLS files: Present', text)

    def test_key_only(self):
        client = mock.Mock()
        client.get = mock.Mock(side_effect=[(b'key', None), (None, None)])
        text = self._render(client)
        self.assertIn('TLS private key: Present in etcd, certificate missing', text)

    def test_cert_only(self):
        client = mock.Mock()
        client.get = mock.Mock(side_effect=[(None, None), (b'cert', None)])
        text = self._render(client)
        self.assertIn('TLS certificate: Present in etcd, key missing', text)

    def test_neither_present(self):
        client = mock.Mock()
        client.get = mock.Mock(side_effect=[(None, None), (None, None)])
        text = self._render(client)
        self.assertIn("TLS materials: Not found (generate with 'ycluster tls generate')", text)

    def test_etcd_error(self):
        with mock.patch.object(certbot, 'get_etcd_client',
                               side_effect=ConnectionError('down')):
            text = self._render(None)
        self.assertIn('TLS materials: Error checking', text)


class LetsEncryptStatusTests(unittest.TestCase):
    def test_cert_present(self):
        client = mock.Mock()
        client.get = mock.Mock(return_value=(b'pem', None))
        out = io.StringIO()
        with mock.patch.object(certbot, 'get_etcd_client', return_value=client):
            with redirect_stdout(out):
                certbot._print_lets_encrypt_status()
        self.assertIn("Let's Encrypt certificate: Present in etcd", out.getvalue())

    def test_cert_missing(self):
        client = mock.Mock()
        client.get = mock.Mock(return_value=(None, None))
        out = io.StringIO()
        with mock.patch.object(certbot, 'get_etcd_client', return_value=client):
            with redirect_stdout(out):
                certbot._print_lets_encrypt_status()
        self.assertIn("Let's Encrypt certificate: Not found", out.getvalue())


if __name__ == '__main__':
    unittest.main()
