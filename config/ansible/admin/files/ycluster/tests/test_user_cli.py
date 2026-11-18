import unittest
from unittest import mock

from ycluster.cli import user
from ycluster.utils import authentik_manager


class UserProjectArgsTests(unittest.TestCase):
    def test_add_remove_set(self):
        current = {'a'}
        self.assertEqual(user._updated_projects(current, ['b'], 'add'), ['a', 'b'])
        self.assertEqual(user._updated_projects(current, ['a'], 'remove'), [])
        self.assertEqual(user._updated_projects(current, ['b', 'b'], 'set'), ['b'])


class UserRecoveryArgsTests(unittest.TestCase):
    def test_days_passed_to_manager(self):
        args = mock.Mock(email='a@example.com', days=7)
        with mock.patch.object(authentik_manager, 'recovery_link',
                               return_value='https://auth.example/link') as link, \
                mock.patch('builtins.print'):
            user.user_recovery(args)
        link.assert_called_once_with('a@example.com', 7)


if __name__ == '__main__':
    unittest.main()
