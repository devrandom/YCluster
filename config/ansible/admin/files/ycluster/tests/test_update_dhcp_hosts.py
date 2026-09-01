"""Tests for static DNS hosts updating."""

import os
import subprocess
import unittest
import unittest.mock as mock

from ycluster.utils import update_dhcp_hosts


class UpdateDhcpHostsTests(unittest.TestCase):
    def response(self, text):
        return mock.Mock(text=text)

    def test_empty_response_warns_and_returns(self):
        get = mock.Mock(return_value=self.response('  '))
        with mock.patch.object(update_dhcp_hosts.requests, 'get', get):
            update_dhcp_hosts.main()
        get.assert_called_once_with('http://localhost:12723/api/hosts',
                                    timeout=10)

    def test_unchanged_hosts_do_not_reload(self):
        get = mock.Mock(return_value=self.response('node\n'))
        temp = mock.MagicMock()
        temp.name = '/tmp/hosts.tmp'
        temp.__enter__.return_value = temp
        with mock.patch.object(update_dhcp_hosts.requests, 'get', get), \
                mock.patch.object(update_dhcp_hosts.tempfile,
                                  'NamedTemporaryFile',
                                  return_value=temp) as temp_file, \
                mock.patch.object(update_dhcp_hosts.os.path, 'exists',
                                  return_value=True), \
                mock.patch.object(update_dhcp_hosts.os, 'chmod'), \
                mock.patch.object(update_dhcp_hosts.filecmp, 'cmp',
                                  return_value=True), \
                mock.patch.object(update_dhcp_hosts.os, 'unlink') as unlink:
            update_dhcp_hosts.main()
        temp_file.assert_called_once_with(mode='w', delete=False)
        temp.write.assert_called_once_with('node\n')
        unlink.assert_called_once_with('/tmp/hosts.tmp')

    def test_request_failure_exits(self):
        get = mock.Mock(side_effect=update_dhcp_hosts.requests
                        .exceptions.RequestException('down'))
        with mock.patch.object(update_dhcp_hosts.requests, 'get', get), \
                self.assertRaises(SystemExit):
            update_dhcp_hosts.main()

    def test_reload_failure_exits(self):
        get = mock.Mock(return_value=self.response('node\n'))
        temp = mock.MagicMock()
        temp.name = '/tmp/hosts.tmp'
        temp.__enter__.return_value = temp
        failure = subprocess.CalledProcessError(1, ['systemctl'])
        with mock.patch.object(update_dhcp_hosts.requests, 'get', get), \
                mock.patch.object(update_dhcp_hosts.tempfile,
                                  'NamedTemporaryFile', return_value=temp), \
                mock.patch.object(update_dhcp_hosts.os.path, 'exists',
                                  return_value=False), \
                mock.patch.object(update_dhcp_hosts.os, 'chmod'), \
                mock.patch.object(update_dhcp_hosts.subprocess, 'run',
                                  side_effect=failure), \
                mock.patch.object(update_dhcp_hosts.os, 'unlink'), \
                self.assertRaises(SystemExit):
            update_dhcp_hosts.main()


if __name__ == '__main__':
    unittest.main()
