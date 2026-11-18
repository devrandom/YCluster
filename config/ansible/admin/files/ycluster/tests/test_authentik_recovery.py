"""Tests for admin-generated password (re)set links."""

import unittest
from unittest import mock

from ycluster.utils import authentik_manager


class RecoveryLinkTests(unittest.TestCase):
    def link(self, days=1):
        api = mock.Mock()
        api.post.return_value = {
            'link': 'http://auth.xc/if/flow/recovery-link/?flow_token=abc'
        }
        with mock.patch.object(authentik_manager, 'AuthentikAPI', return_value=api), \
                mock.patch.object(authentik_manager, '_get_user',
                                  return_value={'pk': 7}), \
                mock.patch.object(authentik_manager, '_link_base_url',
                                  return_value='https://auth.example'):
            url = authentik_manager.recovery_link('user@example.com', days=days)
        return url, api

    def test_requests_requested_expiry(self):
        url, api = self.link(days=7)
        api.post.assert_called_once_with(
            '/core/users/7/recovery/', json={'token_duration': 'days=7'})
        self.assertEqual(
            url, 'https://auth.example/if/flow/recovery-link/?flow_token=abc')

    def test_default_expiry_is_one_day(self):
        _, api = self.link()
        api.post.assert_called_once_with(
            '/core/users/7/recovery/', json={'token_duration': 'days=1'})


if __name__ == '__main__':
    unittest.main()
