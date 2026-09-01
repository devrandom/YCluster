import unittest

from ycluster.utils import vm_manager


class LiveInstanceIPsTest(unittest.TestCase):
    def test_global_inet_only(self):
        inst = {'state': {'network': {
            'eth0': {'addresses': [
                {'family': 'inet', 'scope': 'global', 'address': 'ip-a'},
                {'family': 'inet6', 'scope': 'global', 'address': 'v6'},
                {'family': 'inet', 'scope': 'link', 'address': 'link'},
            ]},
            'lo': {'addresses': []},
        }}}
        self.assertEqual(vm_manager._live_instance_ips(inst), {'ip-a'})

    def test_no_state(self):
        self.assertEqual(vm_manager._live_instance_ips({}), set())


if __name__ == '__main__':
    unittest.main()
