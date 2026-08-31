import os
import tempfile
import unittest
from unittest import mock

import app_autoinstall


class AutoinstallHelperTests(unittest.TestCase):
    def test_env_password_wins(self):
        with mock.patch.dict(os.environ, {'UBUNTU_PASSWORD_HASH': 'env'}):
            self.assertEqual(app_autoinstall.ubuntu_password(), 'env')

    def test_shadow_password(self):
        with tempfile.NamedTemporaryFile(mode='w') as f:
            f.write('root:x\nubuntu:crypted\n')
            f.flush()
            self.assertEqual(app_autoinstall._password_from_shadow(f.name), 'crypted')

    def test_ssh_key(self):
        with tempfile.NamedTemporaryFile(mode='w') as f:
            f.write('key\n')
            f.flush()
            self.assertEqual(app_autoinstall.ssh_key(f.name), 'key')

    def test_render_user_data(self):
        template = mock.Mock()
        template.render.return_value = 'rendered'
        got = app_autoinstall.render_user_data(
            template,
            {'type': 'compute', 'hostname': 'host', 'ip': 'ip', 'amt_ip': 'amt'},
            {'cluster_interface': 'c', 'uplink_interface': 'u', 'amt_interface': 'a'},
            'proxy', 'vip', 'key', 'pass')
        self.assertEqual(got, 'rendered')
        values = template.render.call_args[1]
        self.assertEqual(values['node_type'], 'compute')
        self.assertEqual(values['ubuntu_password'], 'pass')


if __name__ == '__main__':
    unittest.main()


class PasswordFallbackTests(unittest.TestCase):
    def test_missing_shadow_password(self):
        with mock.patch.dict(os.environ, {'UBUNTU_PASSWORD_HASH': ''}), \
                mock.patch.object(app_autoinstall, '_password_from_shadow',
                                  return_value=None):
            with self.assertRaises(Exception):
                app_autoinstall.ubuntu_password()

    def test_shadow_permission_error(self):
        with mock.patch.dict(os.environ, {'UBUNTU_PASSWORD_HASH': ''}), \
                mock.patch('builtins.open', side_effect=PermissionError):
            self.assertIsNone(app_autoinstall._password_from_shadow('/etc/shadow'))

    def test_shadow_user_not_found(self):
        with tempfile.NamedTemporaryFile(mode='w') as f:
            self.assertIsNone(app_autoinstall._password_from_shadow(f.name))

    def test_env_password_empty_means_shadow(self):
        with mock.patch.dict(os.environ, {'UBUNTU_PASSWORD_HASH': ''}), \
                mock.patch.object(app_autoinstall, '_password_from_shadow',
                                  return_value='shadow'):
            self.assertEqual(app_autoinstall.ubuntu_password(), 'shadow')
