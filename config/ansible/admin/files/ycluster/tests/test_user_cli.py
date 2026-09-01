import unittest

from ycluster.cli import user


class UserProjectArgsTests(unittest.TestCase):
    def test_add_remove_set(self):
        current = {'a'}
        self.assertEqual(user._updated_projects(current, ['b'], 'add'), ['a', 'b'])
        self.assertEqual(user._updated_projects(current, ['a'], 'remove'), [])
        self.assertEqual(user._updated_projects(current, ['b', 'b'], 'set'), ['b'])


if __name__ == '__main__':
    unittest.main()
