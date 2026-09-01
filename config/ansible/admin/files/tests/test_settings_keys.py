import unittest
from types import SimpleNamespace
from unittest import mock

import app as appmod


def identity(email='operator'):
    return SimpleNamespace(email=email, is_admin=False, is_manager=False)


class SettingsKeysRoutesTests(unittest.TestCase):
    def setUp(self):
        self.client = appmod.app.test_client()

    def test_add_requires_identity(self):
        with mock.patch.object(appmod, '_authentik_identity', return_value=identity('')):
            response = self.client.post('/admin/settings/keys/add')
        self.assertEqual(response.status_code, 403)

    def test_add_requires_key(self):
        with mock.patch.object(appmod, '_authentik_identity', return_value=identity()):
            response = self.client.post('/admin/settings/keys/add', json={'key': '   '})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {'error': 'no key provided'})

    def test_add_success(self):
        with mock.patch.object(appmod, '_authentik_identity', return_value=identity()), \
             mock.patch.object(appmod.vmm, 'user_add_key') as add_key, \
             mock.patch.object(appmod.vmm, 'user_get', return_value={'ssh_keys': ['key']}):
            response = self.client.post('/admin/settings/keys/add', json={'key': 'key'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'keys': ['key']})
        add_key.assert_called_once_with('operator', 'key')

    def test_remove_success(self):
        with mock.patch.object(appmod, '_authentik_identity', return_value=identity()), \
             mock.patch.object(appmod.vmm, 'user_get', side_effect=[{'ssh_keys': ['key']}, {}]), \
             mock.patch.object(appmod.vmm, 'user_remove_key') as remove_key:
            response = self.client.post('/admin/settings/keys/remove', json={'key': 'key'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'keys': []})
        remove_key.assert_called_once_with('operator', 'key')

    def test_remove_missing_key(self):
        with mock.patch.object(appmod, '_authentik_identity', return_value=identity()), \
             mock.patch.object(appmod.vmm, 'user_get', return_value={'ssh_keys': []}):
            response = self.client.post('/admin/settings/keys/remove', json={'key': 'key'})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()['error'], "etcd write failed: 'key not found'")


if __name__ == '__main__':
    unittest.main()
