import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ycluster.utils import ca_manager


class CaManagerHelpersTests(unittest.TestCase):
    def test_certificate_common_name(self):
        common = mock.Mock()
        common.oid = ca_manager.NameOID.COMMON_NAME
        common.value = 'test-node'
        other = mock.Mock()
        cert = mock.Mock()
        cert.subject = [other, common]
        self.assertEqual(ca_manager._certificate_common_name(cert), 'test-node')

    def test_certificate_common_name_missing(self):
        cert = mock.Mock()
        cert.subject = []
        self.assertIsNone(ca_manager._certificate_common_name(cert))

    def test_certificate_san(self):
        name = mock.Mock(__str__=lambda self: 'test-node')
        ext = mock.Mock()
        ext.value = [name]
        cert = mock.Mock()
        cert.extensions.get_extension_for_oid.return_value = ext
        self.assertEqual(ca_manager._certificate_san(cert), ['test-node'])

    def test_certificate_san_missing(self):
        cert = mock.Mock()
        cert.extensions.get_extension_for_oid.side_effect = ca_manager.x509.ExtensionNotFound('missing', None)
        self.assertEqual(ca_manager._certificate_san(cert), [])

    def test_print_certificate_error(self):
        out = []
        with mock.patch('builtins.print', lambda *args: out.append(args)):
            ca_manager._print_certificate(Path('/nonexistent/test.crt'))
        self.assertIn('  test: Error reading certificate - ', out[-1][0])

    def test_list_certificates_empty(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(ca_manager, 'CERTS_PATH', td), \
                mock.patch('builtins.print') as printed:
            ca_manager.list_certificates()
            printed.assert_any_call('No certificates found')


if __name__ == '__main__':
    unittest.main()
