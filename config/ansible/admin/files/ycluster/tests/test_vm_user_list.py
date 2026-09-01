import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from ycluster.utils import vm_manager


class UserListTests(unittest.TestCase):
    def test_missing_user(self):
        out = io.StringIO()
        with redirect_stdout(out), \
                mock.patch.object(vm_manager, 'user_get', return_value=None):
            vm_manager.user_list('missing')
        self.assertEqual(out.getvalue(), 'No such user: missing\n')

    def test_key_summary_sorted(self):
        users = {'b': {'ssh_keys': ['1', '2']}, 'a': {'ssh_keys': []}}
        out = io.StringIO()
        with redirect_stdout(out), \
                mock.patch.object(vm_manager, 'users_all', return_value=users):
            vm_manager.user_list()
        self.assertEqual(out.getvalue(),
                         'a: 0 key(s)\nb: 2 key(s)\n')


if __name__ == '__main__':
    unittest.main()
