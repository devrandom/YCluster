import unittest
from datetime import datetime, timezone
from unittest import mock

from ycluster.utils import certbot_manager


class FakeEtcd:
    def __init__(self, values):
        self.values = values

    def get(self, key):
        return self.values.get(key), None


class RenewLeaderTests(unittest.TestCase):
    def test_succeeds_as_noop_when_no_storage_leader(self):
        client = FakeEtcd({})
        with mock.patch.object(certbot_manager, 'get_etcd_client', return_value=client), \
             mock.patch.object(certbot_manager, 'get_https_config') as config:
            self.assertTrue(certbot_manager.renew_certificates())
        config.assert_not_called()

    def test_succeeds_as_noop_when_not_storage_leader(self):
        client = FakeEtcd({'/cluster/leader/app': b'leader'})
        with mock.patch.object(certbot_manager, 'get_etcd_client', return_value=client), \
             mock.patch('socket.gethostname', return_value='follower'), \
             mock.patch.object(certbot_manager, 'get_https_config') as config:
            self.assertTrue(certbot_manager.renew_certificates())
        config.assert_not_called()

    def test_runs_on_storage_leader(self):
        client = FakeEtcd({'/cluster/leader/app': b'leader'})
        with mock.patch.object(certbot_manager, 'get_etcd_client', return_value=client), \
             mock.patch('socket.gethostname', return_value='leader'), \
             mock.patch.object(certbot_manager, 'get_https_config', return_value={}) as config:
            self.assertFalse(certbot_manager.renew_certificates())
        config.assert_called_once()


class RenewalWindowTests(unittest.TestCase):
    @staticmethod
    def _openssl_result(stdout):
        return mock.Mock(returncode=0, stdout=stdout, stderr='')

    def test_skips_certificate_with_more_than_30_days_remaining(self):
        output = 'notAfter=Dec  2 08:42:14 2026 GMT\n'
        now = datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc)
        with mock.patch.object(certbot_manager, 'subprocess') as subprocess:
            subprocess.run.return_value = self._openssl_result(output)
            self.assertFalse(certbot_manager._certificate_needs_renewal(b'cert', now=now))

    def test_renews_certificate_within_30_days(self):
        output = 'notAfter=Dec  2 08:42:14 2026 GMT\n'
        now = datetime(2026, 12, 1, 8, 0, tzinfo=timezone.utc)
        with mock.patch.object(certbot_manager, 'subprocess') as subprocess:
            subprocess.run.return_value = self._openssl_result(output)
            self.assertTrue(certbot_manager._certificate_needs_renewal(b'cert', now=now))

    def test_timer_renewal_is_noop_before_renewal_window(self):
        client = FakeEtcd({
            '/cluster/leader/app': b'leader',
            '/cluster/tls/cert': b'cert',
        })
        with mock.patch.object(certbot_manager, 'get_etcd_client', return_value=client), \
             mock.patch('socket.gethostname', return_value='leader'), \
             mock.patch.object(certbot_manager, 'get_https_config',
                               return_value={'domain': 'example.com'}), \
             mock.patch.object(certbot_manager, '_certificate_needs_renewal',
                               return_value=False), \
             mock.patch.object(certbot_manager, 'write_tls_key_to_temp') as write_key, \
             mock.patch.object(certbot_manager, 'run_certbot_command') as run_command:
            self.assertTrue(certbot_manager.renew_certificates())
        write_key.assert_not_called()
        run_command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
