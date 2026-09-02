"""Tests for shared etcd client configuration loading."""

import os
import tempfile
import unittest
from unittest import mock

from ycluster.common import etcd_utils


class EtcdUtilsTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def _with_env_file(self, content):
        env_file = tempfile.NamedTemporaryFile(mode='w', suffix='.env',
                                               delete=False)
        env_file.write(content)
        env_file.close()
        self.addCleanup(os.unlink, env_file.name)
        return mock.patch.object(etcd_utils, 'ETCD_CLIENT_ENV_PATH',
                                 env_file.name)

    def test_hosts_and_tls_from_client_env_file(self):
        with self._with_env_file(
                '# Managed by Ansible. Do not edit.\n'
                'ETCD_HOSTS=host-a:2379, host-b:2379\n'
                'ETCD_CACERT=/etc/ycluster/tls/ca.crt\n'
                'ETCD_CERT=/etc/ycluster/tls/client.crt\n'
                'ETCD_KEY=/etc/ycluster/tls/client.key\n'), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(etcd_utils.get_etcd_hosts(),
                             ['host-a:2379', 'host-b:2379'])
            self.assertEqual(etcd_utils.get_tls_kwargs(), {
                'ca_cert': '/etc/ycluster/tls/ca.crt',
                'cert_cert': '/etc/ycluster/tls/client.crt',
                'cert_key': '/etc/ycluster/tls/client.key',
            })

    def test_live_environment_overrides_client_env_file(self):
        with self._with_env_file('ETCD_HOSTS=host-a:2379\n'), \
                mock.patch.dict(os.environ,
                                {'ETCD_HOSTS': 'host-b:2379'}, clear=True):
            self.assertEqual(etcd_utils.get_etcd_hosts(), ['host-b:2379'])

    def test_missing_client_env_file_uses_local_fallback(self):
        with mock.patch.object(etcd_utils, 'ETCD_CLIENT_ENV_PATH',
                               '/nonexistent/etcd-client.env'):
            self.assertEqual(etcd_utils.get_etcd_hosts(), ['localhost:2379'])
            self.assertEqual(etcd_utils.get_tls_kwargs(), {})
