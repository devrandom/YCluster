"""Tests for lightweight admin API routes and extracted helper imports."""

import unittest
from unittest import mock

import app as appmod


class LightweightRouteTests(unittest.TestCase):
    def setUp(self):
        self.http = appmod.app.test_client()

    def test_ping_time_and_metadata(self):
        self.assertEqual(self.http.get('/api/ping').get_json()['status'], 'ok')
        self.assertIn('timestamp', self.http.get('/api/time').get_json())
        response = self.http.get('/autoinstall/meta-data')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, '')

    def test_allocate_hostname_validation_and_errors(self):
        response = self.http.get('/api/allocate')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {'error': 'MAC address is required'})

        response = self.http.get('/api/allocate?mac=aa:bb&type=bad')
        self.assertEqual(response.status_code, 400)
        self.assertIn('Invalid type', response.get_json()['error'])

        allocation = {
            'hostname': 'c1', 'type': 'compute', 'ip': 'ip-c1',
            'amt_ip': 'ip-amt',
        }
        with mock.patch.object(appmod, 'get_or_create_allocation',
                               return_value=(allocation, True)):
            response = self.http.get('/api/allocate?mac=AA:BB')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            'hostname': 'c1', 'type': 'compute', 'ip': 'ip-c1',
            'amt_ip': 'ip-amt', 'mac': 'AA:BB', 'existing': False,
        })

        with mock.patch.object(appmod, 'get_or_create_allocation',
                               return_value=(allocation, False)):
            response = self.http.get('/api/allocate?mac=AA:BB')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['existing'], True)

        with mock.patch.object(appmod, 'get_or_create_allocation',
                               side_effect=RuntimeError('etcd down')):
            response = self.http.get('/api/allocate?mac=AA:BB')
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json(), {'error': 'Allocation failed: etcd down'})

    def test_inventory_export_csv(self):
        with mock.patch('ycluster.utils.inventory.list_all',
                        return_value=[{'host': 'host-a'}]), \
                mock.patch.object(appmod.app_inventory, 'export_csv') as export:
            export.return_value.getvalue.return_value = 'host,type\n'
            response = self.http.get('/api/inventory/export.csv')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'text/csv')
        self.assertEqual(response.text, 'host,type\n')

    def test_vm_usage_page_clamps_since_days(self):
        response = self.http.get('/admin/vm-usage?since=999')
        self.assertEqual(response.status_code, 200)

    def test_alert_webhook_acknowledges_alerts(self):
        payload = {'alerts': [
            {'status': 'firing', 'labels': {
                'alertname': 'HighMemory', 'severity': 'warning',
                'node': 'host-a'}},
        ]}
        with mock.patch.object(appmod, 'print') as mock_print:
            response = self.http.post('/api/alert-webhook', json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'status': 'received'})
        mock_print.assert_called_once()

    def test_hosts_file_rendering(self):
        client = mock.Mock()
        with mock.patch.object(appmod, 'get_etcd_client', return_value=client), \
                mock.patch.object(appmod, '_node_hosts_entries',
                                  return_value=['ip-node node1 node1.xc']), \
                mock.patch.object(appmod, '_frontend_hosts_entries',
                                  return_value=['ip-frontend frontend frontend.xc']):
            response = self.http.get('/api/hosts')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'text/plain')
        self.assertIn('ip-node node1 node1.xc', response.text)
        self.assertIn('ip-frontend frontend frontend.xc', response.text)
        self.assertIn(f'{appmod.STORAGE_VIP} forgejo.xc', response.text)


if __name__ == '__main__':
    unittest.main()
