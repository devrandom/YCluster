"""Tests that exercise CLI registration and thin command wrappers."""

import argparse
import io
import json
import contextlib
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from ycluster.cli import dhcp
from ycluster.cli import frontend
from ycluster.cli import frp
from ycluster.cli import healthchecks
from ycluster.cli import tls
from ycluster.cli import vm
from ycluster.utils import ca_manager
from ycluster.utils import fetch_tls_certs
from ycluster.utils import update_dhcp_hosts


def call(func, **kwargs):
    args = SimpleNamespace(parser=None, **kwargs)
    return func(args)


class ParserSpy:
    def __init__(self):
        self.defaults = {}
        self.arguments = []

    def add_argument(self, *args, **kwargs):
        self.arguments.append(args)

    def set_defaults(self, **kwargs):
        self.defaults.update(kwargs)


class DHCPTests(unittest.TestCase):
    def test_registration_and_commands(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        dhcp.register_dhcp_commands(subparsers)
        dhcp_parser = subparsers.choices['dhcp']

        with mock.patch.object(dhcp.lease_manager, 'list_allocations') as la, \
                mock.patch.object(dhcp.lease_manager, 'list_leases') as ll:
            call(dhcp.dhcp_list_all, unused=None)
            self.assertEqual(la.call_count, 1)
            self.assertEqual(ll.call_count, 1)

        with mock.patch.object(dhcp.lease_manager, 'delete_by_mac',
                               return_value=True) as delete:
            call(dhcp.dhcp_delete, target='aa-bb-cc-dd-ee-ff')
            delete.assert_called_once_with('aa-bb-cc-dd-ee-ff')

        with mock.patch.object(dhcp.lease_manager, 'delete_by_hostname',
                               return_value=False) as delete, \
                mock.patch('sys.stderr'):
            with self.assertRaises(SystemExit):
                call(dhcp.dhcp_delete, target='host-a')
            delete.assert_called_once_with('host-a')

        with mock.patch.object(dhcp.lease_manager, 'list_allocations') as la, \
                mock.patch.object(dhcp.lease_manager, 'list_leases') as ll:
            call(dhcp.dhcp_list_allocations)
            call(dhcp.dhcp_list_leases)
        self.assertEqual(la.call_count, 1)
        self.assertEqual(ll.call_count, 1)
        with mock.patch.object(update_dhcp_hosts, 'main') as update:
            call(dhcp.dhcp_update_hosts)
            update.assert_called_once_with()


class FrontendTests(unittest.TestCase):
    def test_registration_and_commands(self):
        parser = argparse.ArgumentParser()
        frontend.register_frontend_commands(parser.add_subparsers())
        with mock.patch.object(frontend.frontend_manager,
                               'list_frontend_nodes') as listed:
            call(frontend.frontend_list)
            listed.assert_called_once_with()
        with mock.patch.object(frontend.frontend_manager,
                               'add_frontend_node') as added:
            call(frontend.frontend_add, name='a', address='host-a',
                 description='desc')
            added.assert_called_once_with('a', 'host-a', 'desc')
        with mock.patch.object(frontend.frontend_manager,
                               'delete_frontend_node') as deleted:
            call(frontend.frontend_delete, name='a')
            deleted.assert_called_once_with('a')
        with mock.patch.object(frontend.frontend_manager,
                               'show_frontend_node') as shown:
            call(frontend.frontend_show, name='a')
            shown.assert_called_once_with('a')


class FrpTests(unittest.TestCase):
    def test_registration_and_commands(self):
        parser = argparse.ArgumentParser()
        frp.register_frp_commands(parser.add_subparsers())
        calls = [
            (frp.frp_set, 'set_frp_config', ['server.test:7000'], 'token'),
            (frp.frp_get, 'get_frp_config', [], None),
            (frp.frp_generate_client, 'generate_client_config', [], None),
            (frp.frp_generate_ssh_client, 'generate_ssh_client_config', [],
             None),
            (frp.frp_delete, 'delete_frp_config', [], None),
        ]
        for func, method, args, token in calls:
            with mock.patch.object(frp.frp_config, method) as mocked:
                call(func, remote_addr=args[0] if args else None, token=token)
                mocked.assert_called_once()


class VMTests(unittest.TestCase):
    def test_registration(self):
        parser = argparse.ArgumentParser()
        vm.register_vm_commands(parser.add_subparsers())

    def test_ssh_and_print_helpers(self):
        with mock.patch.object(vm.vm, 'user_add_key') as add, \
                mock.patch.object(vm.vm, 'vm_sync_keys') as sync, \
                mock.patch.object(vm.vm, 'bastion_sync') as bastion:
            vm._ssh_add(SimpleNamespace(user='user', key=['a', 'b']))
            add.assert_called_once_with('user', 'a b')
            sync.assert_called_once_with('user')
            bastion.assert_called_once_with()
        with mock.patch.object(vm.vm, 'user_remove_key') as remove, \
                mock.patch.object(vm.vm, 'vm_sync_keys') as sync, \
                mock.patch.object(vm.vm, 'bastion_sync') as bastion:
            vm._ssh_remove(SimpleNamespace(user='user', key='key'))
            remove.assert_called_once_with('user', 'key')
            sync.assert_called_once_with('user')
            bastion.assert_called_once_with()

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            vm._print_dns_sync({})
            vm._print_dns_sync({'br0': ['host-record=a,host-a,ip-a']})
            vm._print_pins([])
            vm._print_pins([('vm1', 'ip-a', 'kept current')])
        self.assertIn('No changes', output.getvalue())
        self.assertIn('br0: 1 host-record(s)', output.getvalue())
        self.assertIn('Pinned 1 instance(s)', output.getvalue())
        self.assertIn('next DHCP renewal', output.getvalue())

    def test_idle_command(self):
        with mock.patch.object(vm.vm_idle, 'report', return_value=[]) as report, \
                mock.patch.object(vm.vm_idle, 'format_report',
                                  return_value='table'), \
                mock.patch.object(vm.vm_idle, 'notify') as notify:
            call(vm._vm_idle, notify=True, stream='ops', hours=24,
                 mute_owner=None, unmute_owner=None)
        report.assert_called_once_with(idle_after=timedelta(hours=24))
        notify.assert_called_once_with([], idle_after=timedelta(hours=24),
                                      stream='ops')

    def test_idle_command_without_notify(self):
        with mock.patch.object(vm.vm_idle, 'report', return_value=[]), \
                mock.patch.object(vm.vm_idle, 'format_report',
                                  return_value='table'), \
                mock.patch.object(vm.vm_idle, 'notify') as notify:
            call(vm._vm_idle, notify=False, stream=None, hours=24,
                 mute_owner=None, unmute_owner=None)
        notify.assert_not_called()

    def test_idle_mute_owner(self):
        with mock.patch.object(vm.vm_idle, 'set_suppressed',
                                  return_value=['a@example.test']) as mute, \
                mock.patch.object(vm.vm_idle, 'report') as report:
            call(vm._vm_idle, notify=False, stream=None, hours=24,
                 mute_owner='a@example.test', unmute_owner=None)
        mute.assert_called_once_with('a@example.test', True)
        report.assert_not_called()

    def test_idle_unmute_owner(self):
        with mock.patch.object(vm.vm_idle, 'set_suppressed',
                                  return_value=[]) as mute:
            call(vm._vm_idle, notify=False, stream=None, hours=24,
                 mute_owner=None, unmute_owner='a@example.test')
        mute.assert_called_once_with('a@example.test', False)


class TLSTests(unittest.TestCase):
    def test_registration_and_wrappers(self):
        parser = argparse.ArgumentParser()
        tls.register_tls_commands(parser.add_subparsers())
        with mock.patch.object(tls.tls_config, 'generate_and_store_cert') as gen:
            call(tls.tls_generate, common_name='cluster', san=None)
            self.assertEqual(gen.call_args.args, ('cluster', [
                '10.0.' + '0.254', 'cluster.local', 'localhost']))
        with mock.patch.object(tls.tls_config, 'set_common_name_in_etcd') as cn:
            call(tls.tls_set_common_name, common_name='cluster')
            cn.assert_called_once_with('cluster')

        with mock.patch.object(tls.tls_config, 'get_common_name_from_etcd',
                               return_value='cluster'):
            call(tls.tls_get_common_name)
        with mock.patch.object(tls.tls_config, 'get_tls_cert',
                               return_value=False), \
                self.assertRaises(SystemExit):
            call(tls.tls_get)
        with mock.patch.object(tls.tls_config, 'delete_tls_cert') as delete:
            call(tls.tls_delete)
            delete.assert_called_once_with()


class TLSWrapperTests(unittest.TestCase):
    def test_file_and_ca_wrappers(self):
        with mock.patch.object(tls.tls_config, 'get_common_name_from_etcd',
                               return_value=None):
            call(tls.tls_get_common_name)
        with mock.patch.object(tls.tls_config, 'set_tls_cert') as set_cert, \
                mock.patch('builtins.open', mock.mock_open(read_data='pem')):
            call(tls.tls_set, cert_file='cert', key_file='key')
            set_cert.assert_called_once_with('pem', 'pem')
        with mock.patch.object(tls.tls_config, 'get_tls_cert',
                               return_value=True):
            call(tls.tls_get)
        with mock.patch.object(fetch_tls_certs, 'main') as fetch:
            call(tls.tls_fetch_certs)
            fetch.assert_called_once_with()

    def test_ca_wrappers(self):
        with mock.patch.object(ca_manager, 'generate_ca') as gen:
            call(tls.ca_generate)
            gen.assert_called_once_with()
        with mock.patch.object(ca_manager, 'get_ca_info',
                               return_value=True):
            call(tls.ca_info)
        with mock.patch.object(ca_manager, 'generate_server_cert') as server:
            call(tls.ca_generate_server, hostname='host-a', san=['san'])
            server.assert_called_once_with('host-a', ['san'])
        with mock.patch.object(ca_manager, 'list_certificates') as listed:
            call(tls.ca_list)
            listed.assert_called_once_with()
        with mock.patch.object(ca_manager, 'revoke_certificate') as revoke:
            call(tls.ca_revoke, hostname='host-a')
            revoke.assert_called_once_with('host-a')

    def test_ca_error_paths(self):
        for func, method, args in [
                (tls.ca_generate, 'generate_ca', {}),
                (tls.ca_generate_server, 'generate_server_cert',
                 {'hostname': 'host-a', 'san': []}),
                (tls.ca_revoke, 'revoke_certificate',
                 {'hostname': 'host-a'})]:
            with mock.patch.object(ca_manager, method,
                                   side_effect=RuntimeError('boom')), \
                    mock.patch('sys.stderr'):
                with self.assertRaises(SystemExit):
                    call(func, **args)
        with mock.patch.object(ca_manager, 'get_ca_info',
                               return_value=False), \
                self.assertRaises(SystemExit):
            call(tls.ca_info)


class FakeEtcdClient:
    def __init__(self, error=None):
        self.values = {}
        self.error = error

    def put(self, key, value):
        if self.error:
            raise self.error
        self.values[key] = value

    def get(self, key):
        if self.error:
            raise self.error
        value = self.values.get(key)
        return (value.encode(), None) if value else (None, None)

    def delete(self, key):
        if self.error:
            raise self.error
        self.values.pop(key, None)


class HealthchecksTests(unittest.TestCase):
    def setUp(self):
        parser = argparse.ArgumentParser()
        healthchecks.register_healthchecks_commands(parser.add_subparsers())
        self.parser = parser

    def run_cli(self, argv, client):
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(healthchecks, 'get_etcd_client',
                               return_value=client):
            try:
                args = self.parser.parse_args(argv)
                with contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(err):
                    args.func(args)
            except SystemExit as exc:
                self.assertEqual(exc.code, 1)
                return out.getvalue(), err.getvalue(), True
        return out.getvalue(), err.getvalue(), False

    def test_registration_and_url_commands(self):
        client = FakeEtcdClient()
        out, err, failed = self.run_cli(
            ['healthchecks', 'set-url', ' https://example.test/ping '],
            client)
        self.assertFalse(failed)
        self.assertIn('https://example.test/ping', out)
        self.assertEqual(
            client.values[healthchecks.ETCD_KEY],
            'https://example.test/ping')

        out, err, failed = self.run_cli(['healthchecks', 'get-url'], client)
        self.assertFalse(failed)
        self.assertIn('Healthchecks URL: https://example.test/ping', out)

        out, err, failed = self.run_cli(['healthchecks', 'delete-url'], client)
        self.assertFalse(failed)
        self.assertIn('Healthchecks URL deleted', out)
        self.assertNotIn(healthchecks.ETCD_KEY, client.values)

    def test_missing_url_error_paths(self):
        out, err, failed = self.run_cli(
            ['healthchecks', 'set-url', 'bad'], FakeEtcdClient())
        self.assertTrue(failed)
        self.assertIn('URL must start with', err)

        out, err, failed = self.run_cli(
            ['healthchecks', 'get-url'], FakeEtcdClient())
        self.assertTrue(failed)
        self.assertIn('No healthchecks URL configured', out)

        out, err, failed = self.run_cli(
            ['healthchecks', 'delete-url'], FakeEtcdClient())
        self.assertTrue(failed)
        self.assertIn('No healthchecks URL configured', out)

    def test_etcd_error_paths(self):
        for argv in [
                ['healthchecks', 'set-url', 'https://example.test/ping'],
                ['healthchecks', 'get-url'],
                ['healthchecks', 'delete-url'],
                ['healthchecks', 'test']]:
            out, err, failed = self.run_cli(argv, FakeEtcdClient(error=RuntimeError('boom')))
            self.assertTrue(failed)
            self.assertIn('boom', err)

    def test_test_ping_result(self):
        client = FakeEtcdClient()
        client.values[healthchecks.ETCD_KEY] = 'https://example.test/ping'
        response = SimpleNamespace(status_code=200)
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(healthchecks, 'get_etcd_client',
                               return_value=client), \
                mock.patch('requests.post', return_value=response), \
                mock.patch('socket.gethostname', return_value='host-a'):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                args = self.parser.parse_args(['healthchecks', 'test'])
                args.func(args)
        self.assertIn('Test ping sent successfully', out.getvalue())

        client.values[healthchecks.ETCD_KEY] = 'https://example.test/ping'
        out = io.StringIO()
        err = io.StringIO()
        failed = False
        with mock.patch.object(healthchecks, 'get_etcd_client',
                               return_value=client), \
                mock.patch('requests.post',
                           return_value=SimpleNamespace(status_code=500)), \
                mock.patch('socket.gethostname', return_value='host-a'):
            try:
                args = self.parser.parse_args(['healthchecks', 'test'])
                with contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(err):
                    args.func(args)
            except SystemExit as exc:
                failed = exc.code == 1
        self.assertTrue(failed)
        self.assertIn('Test ping failed with HTTP 500', err.getvalue())

    def test_test_ping_requires_url(self):
        out, err, failed = self.run_cli(['healthchecks', 'test'],
                                        FakeEtcdClient())
        self.assertTrue(failed)
        self.assertIn('No healthchecks URL configured', err)


if __name__ == '__main__':
    unittest.main()
