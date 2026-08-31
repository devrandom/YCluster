import unittest
from unittest import mock

from ycluster.utils import frp_config


class FrpConfigTests(unittest.TestCase):
    def test_validate_host_port(self):
        frp_config.validate_host_port('server:7000')
        with self.assertRaises(ValueError):
            frp_config.validate_host_port('server')

    def test_merged_config_uses_existing_for_partial_update(self):
        client = mock.Mock()
        client.get.return_value = (b'{"remote_addr": "old:7000", "token": "old"}', None)
        got = frp_config._merged_frp_config(client, None, 'new')
        self.assertEqual(got, {'remote_addr': 'old:7000', 'token': 'new'})

    def test_merged_config_ignores_existing_when_full_update(self):
        client = mock.Mock()
        got = frp_config._merged_frp_config(client, 'new:7000', 'new')
        self.assertEqual(got, {'remote_addr': 'new:7000', 'token': 'new'})
        client.get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
