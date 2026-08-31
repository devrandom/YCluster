import unittest
from unittest import mock

import app_schedule
from ycluster.utils import vm_manager as vmm


class ScheduleHelpersTests(unittest.TestCase):
    def test_live_state(self):
        states = {'h': {'gpu_pool': 4, 'instances': [{'name': 'vm', 'status': 'Running'}]}}
        live, pools = app_schedule.live_state(states)
        self.assertEqual(live, {'vm': 'Running'})
        self.assertEqual(pools, {'h': 4})

    def test_schedule_row_fields(self):
        id = mock.Mock(is_manager=True)
        rec = {'owner': 'owner-user', 'host': 'h', 'gpus': 2, 'type': 'vm'}
        desired = {'mode': 'schedule', 'windows': []}
        with mock.patch.object(vmm, 'bastion_public_port', return_value=1234):
            row = app_schedule.schedule_row(
                id, 'vm', rec, desired, {'vm': {'op': 'start'}},
                {'vm': {'warned_at': 'now'}}, {'vm': 'Running'},
                'now', lambda *a: True, vmm)
        self.assertEqual(row['vm'], 'vm')
        self.assertEqual(row['sched_gpus'], 2)
        self.assertEqual(row['port'], 1234)
        self.assertTrue(row['can_edit'])

    def test_schedule_hosts_manager_block_host(self):
        id = mock.Mock(is_manager=True, is_admin=False)
        rows = [{'host': 'h'}]
        blocks = {'block': {'host': 'block-host'}}
        got = app_schedule.schedule_hosts(id, rows, blocks, {'block-host': [{'vm': 'block', 'gpus': 1, 'mode': 'on', 'owner': None, 'windows': None, 'is_block': True}]},
                                          {'h': 2}, lambda o: True)
        self.assertEqual([h['host'] for h in got], ['block-host', 'h'])

    def test_render_commitments_redaction(self):
        commits = [{'vm': 'vm', 'gpus': 1, 'mode': 'on', 'owner': 'secret',
                    'windows': None, 'is_block': False}]
        got = app_schedule.render_commitments(commits, lambda o: False)
        self.assertIsNone(got[0]['owner'])


if __name__ == '__main__':
    unittest.main()
