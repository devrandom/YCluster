"""Tests for PostgreSQL usage_stats provisioning."""

import importlib.util
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    'provision_usage_stats', _ROOT / 'provision-usage-stats.py')
provision = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(provision)


class ProvisionPsqlTests(unittest.TestCase):
    def test_psql_success(self):
        result = mock.Mock(returncode=0, stdout='', stderr='')
        with mock.patch.object(provision.subprocess, 'run',
                               return_value=result) as run:
            returned = provision.psql('SELECT 1;', db='db', check=True)
            self.assertEqual(returned, result)
        self.assertEqual(run.call_args.kwargs['input'], 'SELECT 1;')

    def test_psql_ignored_and_fatal_errors(self):
        ignored = mock.Mock(returncode=1, stdout='', stderr='already exists')
        with mock.patch.object(provision.subprocess, 'run',
                               return_value=ignored):
            self.assertEqual(provision.psql('CREATE TABLE x;'), ignored)

        fatal = mock.Mock(returncode=1, stdout='', stderr='boom')
        with mock.patch.object(provision.subprocess, 'run',
                               return_value=fatal), \
                mock.patch('sys.stderr'):
            self.assertEqual(provision.psql('CREATE TABLE x;'), fatal)
            with self.assertRaises(SystemExit):
                provision.psql('CREATE TABLE x;', check=True)

    def test_get_password(self):
        client = mock.Mock()
        client.get.return_value = (b'secret', None)
        with mock.patch.object(provision, 'get_etcd_client',
                               return_value=client):
            self.assertEqual(provision.get_password(), 'secret')
        client.get.return_value = (None, None)
        with mock.patch.object(provision, 'get_etcd_client',
                               return_value=client):
            with self.assertRaises(Exception):
                provision.get_password()

    def test_main_provisions_database(self):
        commands = []

        def psql(cmd, db=None, check=False):
            commands.append((cmd, db, check))
            return mock.Mock(returncode=0)

        content = 'host    usage_stats\n'
        with mock.patch.object(provision, 'get_password',
                               return_value="pa'ss"), \
                mock.patch.object(provision, 'psql', side_effect=psql), \
                mock.patch('builtins.open',
                           mock.mock_open(read_data=content)) as open_file, \
                mock.patch.object(provision.subprocess, 'run') as run:
            provision.main()

        self.assertTrue(any('CREATE USER usage_stats' in cmd
                            for cmd, _, _ in commands))
        self.assertTrue(any(db == 'usage_stats' for _, db, _ in commands))
        self.assertTrue(open_file.call_count >= 1)
        run.assert_called_once_with(
            ['systemctl', 'reload', 'postgresql@16-main'],
            capture_output=True)

    def test_main_skips_reload_when_no_existing_entry(self):
        with mock.patch.object(provision, 'get_password',
                               return_value='secret'), \
                mock.patch.object(provision, 'psql'), \
                mock.patch('builtins.open',
                           mock.mock_open(read_data='other\n')), \
                mock.patch.object(provision.subprocess, 'run') as run:
            provision.main()
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
