"""Tests for wg CLI peer listing helpers."""

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from ycluster.cli import wg


def peer(hostname, status, ip='ip-a'):
    node = {'ip': ip, 'type': 'vm'}
    return (hostname, {'status': status}, node)


class FilterPeersTests(unittest.TestCase):
    def setUp(self):
        self.peers = [peer('a', 'approved'), peer('b', 'pending'),
                      peer('c', 'approved')]

    def test_no_filter(self):
        args = mock.Mock(pending=False, approved=False)
        self.assertEqual(len(wg._filter_peers(self.peers, args)), 3)

    def test_pending(self):
        args = mock.Mock(pending=True, approved=False)
        result = wg._filter_peers(self.peers, args)
        self.assertEqual([h for h, _, _ in result], ['b'])

    def test_approved(self):
        args = mock.Mock(pending=False, approved=True)
        result = wg._filter_peers(self.peers, args)
        self.assertEqual([h for h, _, _ in result], ['a', 'c'])


class PeerJsonTests(unittest.TestCase):
    def test_with_node(self):
        p = peer('vm1', 'approved')
        out = wg._peer_json(*p)
        self.assertEqual(out['hostname'], 'vm1')
        self.assertEqual(out['ip'], 'ip-a')

    def test_without_node(self):
        out = wg._peer_json('vm1', {'status': 'pending'}, None)
        self.assertIsNone(out['ip'])
        self.assertIsNone(out['type'])


class PrintPeersTableTests(unittest.TestCase):
    def test_output(self):
        out = io.StringIO()
        with redirect_stdout(out):
            wg._print_peers_table([peer('vm1', 'approved')])
        self.assertIn('vm1', out.getvalue())
        self.assertIn('approved', out.getvalue())


if __name__ == '__main__':
    unittest.main()
