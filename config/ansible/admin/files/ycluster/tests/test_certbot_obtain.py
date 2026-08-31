import unittest
from unittest import mock

from ycluster.utils import certbot_manager


class ObtainCertificateValidationTests(unittest.TestCase):
    def test_requires_domain_and_primary(self):
        self.assertEqual(certbot_manager._validate_obtain_domains({}), (None, None, None))

    def test_extracts_primary_and_email(self):
        config = {'domain': 'example.test', 'email': 'ops@example.test'}
        domains, primary, email = certbot_manager._validate_obtain_domains(config)
        self.assertEqual(domains, ['example.test', 'admin.example.test'])
        self.assertEqual(primary, 'example.test')
        self.assertEqual(email, 'ops@example.test')


class PrepareCsrTests(unittest.TestCase):
    def test_cleans_key_when_csr_fails(self):
        with mock.patch.object(certbot_manager, 'write_tls_key_to_temp', return_value='key'), \
             mock.patch.object(certbot_manager, 'generate_csr', return_value=None), \
             mock.patch('os.unlink') as unlink:
            got = certbot_manager._prepare_obtain_csr(['example.test'])
        self.assertEqual(got, (None, None))
        unlink.assert_called_once_with('key')


if __name__ == '__main__':
    unittest.main()
