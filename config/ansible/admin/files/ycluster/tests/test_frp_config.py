import contextlib
import unittest
from unittest import mock

from ycluster.utils import frp_config


class FrpConfigTests(unittest.TestCase):
    def test_validate_host_port(self):
        frp_config.validate_host_port('server:7000')
        with self.assertRaises(ValueError):
            frp_config.validate_host_port('server')

    def test_merged_config_uses_existing_for_partial_update(self):
        client = mock.Mock()
        client.get.return_value = (b'{"remote_addr": "old:7000", "token": "old"}', None)
        got = frp_config._merged_frp_config(client, None, 'new')
        self.assertEqual(got, {'remote_addr': 'old:7000', 'token': 'new'})

    def test_merged_config_ignores_existing_when_full_update(self):
        client = mock.Mock()
        got = frp_config._merged_frp_config(client, 'new:7000', 'new')
        self.assertEqual(got, {'remote_addr': 'new:7000', 'token': 'new'})
        client.get.assert_not_called()


class FrpMainTests(unittest.TestCase):
    def run_main(self, *argv):
        with mock.patch.object(frp_config.sys, 'argv', ['frp', *argv]), \
                mock.patch('builtins.print'):
            frp_config.main()

    def test_no_command_prints_help(self):
        with mock.patch.object(frp_config.argparse.ArgumentParser,
                               'print_help') as help_:
            self.run_main()
        help_.assert_called_once_with()

    def test_commands_dispatch(self):
        calls = []
        handlers = {
            'set_frp_config': lambda *args: calls.append(('set', args)),
            'get_frp_config': lambda: calls.append('get'),
            'generate_client_config': lambda: calls.append('generate-client'),
            'generate_ssh_client_config': lambda: calls.append(
                'generate-ssh-client'),
            'delete_frp_config': lambda: calls.append('delete'),
        }
        patches = [mock.patch.object(frp_config, name, handler)
                   for name, handler in handlers.items()]
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch('builtins.print'))
            for patch in patches:
                stack.enter_context(patch)
            self.run_main('get')
            self.run_main('generate-client')
            self.run_main('generate-ssh-client')
            self.run_main('delete')
            self.run_main('set', '--remote-addr', 'server:7000',
                          '--token', 'token')
        self.assertEqual(calls, [
            'get', 'generate-client', 'generate-ssh-client', 'delete',
            ('set', ('server:7000', 'token'))])

    def test_command_error_exits(self):
        with mock.patch.object(frp_config, 'get_frp_config',
                               side_effect=RuntimeError('unavailable')), \
                self.assertRaises(SystemExit):
            self.run_main('get')


if __name__ == '__main__':
    unittest.main()
