"""Tests for etcd-backed TLS certificate management."""

import argparse
import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest import mock

from cryptography import x509

from ycluster.utils import tls_config


class FakeClient:
    def __init__(self, values=None):
        self.values = values or {}
        self.puts = []
        self.deletes = []

    def get(self, key):
        return self.values.get(key), None

    def put(self, key, value):
        self.puts.append((key, value))

    def delete(self, key):
        return self.values.pop(key, None)


class TLSConfigTests(unittest.TestCase):
    def test_generate_self_signed_cert(self):
        cert_pem, key_pem = tls_config.generate_self_signed_cert(
            'cluster.test', ['alt.test', 'alt2.test'])
        self.assertIn('BEGIN CERTIFICATE', cert_pem)
        self.assertIn('PRIVATE KEY', key_pem)
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        self.assertIn('cluster.test', cert.subject.rfc4514_string())
        san = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value
        self.assertIn('alt.test', san.get_values_for_type(x509.DNSName))
        self.assertIn('cluster.test', cert.subject.rfc4514_string())

    def test_set_get_and_delete_tls_cert(self):
        client = FakeClient()
        cert_pem, _ = tls_config.generate_self_signed_cert('cluster.test')
        with mock.patch.object(tls_config, 'get_etcd_client',
                               return_value=client), \
                contextlib.redirect_stdout(io.StringIO()):
            tls_config.set_tls_cert('cert', 'key')
            client.values = {'/cluster/tls/cert': cert_pem.encode(),
                             '/cluster/tls/key': b'key'}
            self.assertTrue(tls_config.get_tls_cert())
            self.assertIsNone(tls_config.delete_tls_cert())
            self.assertFalse(tls_config.get_tls_cert())
            self.assertIsNone(tls_config.delete_tls_cert())
        self.assertEqual(client.puts, [('/cluster/tls/cert', 'cert'),
                                       ('/cluster/tls/key', 'key')])

    def test_common_name(self):
        client = FakeClient({'/cluster/tls/common_name': b' cluster.test '})
        with mock.patch.object(tls_config, 'get_etcd_client',
                               return_value=client):
            self.assertEqual(tls_config.get_common_name_from_etcd(),
                             'cluster.test')
            tls_config.set_common_name_in_etcd('cluster2.test')
        self.assertEqual(client.puts,
                         [('/cluster/tls/common_name', 'cluster2.test')])

        failing = SimpleNamespace(get=mock.Mock(
            side_effect=RuntimeError('boom')))
        with mock.patch.object(tls_config, 'get_etcd_client',
                               return_value=failing), \
                mock.patch('sys.stderr'):
            self.assertIsNone(tls_config.get_common_name_from_etcd())

    def test_generate_and_store_cert(self):
        client = FakeClient({'/cluster/tls/common_name': b'from.etcd '})
        with mock.patch.object(tls_config, 'get_etcd_client',
                               return_value=client), \
                contextlib.redirect_stdout(io.StringIO()):
            tls_config.generate_and_store_cert(None, ['alt.test'])
            tls_config.generate_and_store_cert(None, None)
        stored = dict(
            (key, value) for key, value in client.puts if key.endswith('name'))
        self.assertEqual(stored['/cluster/tls/common_name'], 'from.etcd')

    def test_main_dispatches_commands(self):
        with mock.patch.object(tls_config, 'generate_and_store_cert') as gen, \
                mock.patch.object(tls_config.sys, 'argv', ['tls', 'generate',
                                        '--common-name', 'cluster.test',
                                        '--san', 'alt.test']):
            tls_config.main()
            gen.assert_called_once_with('cluster.test', ['alt.test'])

        with mock.patch.object(tls_config, 'set_common_name_in_etcd') as cn, \
                mock.patch.object(tls_config.sys, 'argv', ['tls', 'set-common-name',
                                        'cluster.test']):
            tls_config.main()
            cn.assert_called_once_with('cluster.test')

        with mock.patch.object(tls_config, 'get_common_name_from_etcd',
                               return_value='cluster.test'), \
                mock.patch.object(tls_config.sys, 'argv', ['tls', 'get-common-name']), \
                contextlib.redirect_stdout(io.StringIO()):
            tls_config.main()

        with mock.patch.object(tls_config, 'set_tls_cert') as set_cert, \
                mock.patch('builtins.open', mock.mock_open(read_data='pem')), \
                mock.patch.object(tls_config.sys, 'argv', ['tls', 'set', '--cert-file', 'c',
                                        '--key-file', 'k']):
            tls_config.main()
            set_cert.assert_called_once_with('pem', 'pem')

        with mock.patch.object(tls_config, 'get_tls_cert', return_value=True), \
                mock.patch.object(tls_config.sys, 'argv', ['tls', 'get']):
            tls_config.main()
        with mock.patch.object(tls_config, 'get_tls_cert', return_value=False), \
                mock.patch.object(tls_config.sys, 'argv', ['tls', 'get']), \
                self.assertRaises(SystemExit):
            tls_config.main()

        with mock.patch.object(tls_config, 'delete_tls_cert') as delete, \
                mock.patch.object(tls_config.sys, 'argv', ['tls', 'delete']):
            tls_config.main()
            delete.assert_called_once_with()

    def test_main_help_and_error(self):
        output = io.StringIO()
        with mock.patch.object(tls_config.sys, 'argv', ['tls']), \
                contextlib.redirect_stdout(output):
            tls_config.main()
        self.assertIn('usage:', output.getvalue())

        with mock.patch.object(tls_config, 'delete_tls_cert',
                               side_effect=RuntimeError('boom')), \
                mock.patch.object(tls_config.sys, 'argv', ['tls', 'delete']), \
                mock.patch('sys.stderr'):
            with self.assertRaises(SystemExit):
                tls_config.main()


if __name__ == '__main__':
    unittest.main()
