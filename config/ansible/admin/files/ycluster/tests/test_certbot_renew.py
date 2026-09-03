import unittest
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


if __name__ == '__main__':
    unittest.main()
