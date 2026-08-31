"""Tests for the decomposed Tang service check."""

import unittest
from unittest import mock

import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_API_PATH = os.path.join(_HERE, '..', 'health-api.py')
_spec = importlib.util.spec_from_file_location('health_api', _API_PATH)
ha = importlib.util.module_from_spec(_spec)
sys.modules['health_api'] = ha
_spec.loader.exec_module(ha)


class TangAdvertisementTests(unittest.TestCase):
    def test_200_with_key_count(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {'keys': [1, 2, 3]}
        with mock.patch.object(ha.requests, 'get', return_value=response):
            healthy, keys, error = ha._tang_advertisement_status()
        self.assertTrue(healthy)
        self.assertEqual(keys, 3)
        self.assertIsNone(error)

    def test_200_with_unexpected_payload(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = 'not a dict'
        with mock.patch.object(ha.requests, 'get', return_value=response):
            healthy, keys, error = ha._tang_advertisement_status()
        self.assertTrue(healthy)
        self.assertEqual(keys, 'unknown')
        self.assertIsNone(error)

    def test_non_200(self):
        response = mock.Mock(status_code=500)
        with mock.patch.object(ha.requests, 'get', return_value=response):
            healthy, keys, error = ha._tang_advertisement_status()
        self.assertFalse(healthy)
        self.assertIn('HTTP 500', error)

    def test_timeout(self):
        import requests
        with mock.patch.object(ha.requests, 'get',
                               side_effect=requests.exceptions.Timeout()):
            healthy, keys, error = ha._tang_advertisement_status()
        self.assertFalse(healthy)
        self.assertIn('timeout', error)


class TangHealthFromProbeTests(unittest.TestCase):
    def test_healthy(self):
        status, msg = ha._tang_health_from_probe(True, True, True, 3, None)
        self.assertEqual(status, 'healthy')
        self.assertIn('3 keys', msg)

    def test_degraded(self):
        status, msg = ha._tang_health_from_probe(True, True, False, None, 'bad')
        self.assertEqual(status, 'degraded')
        self.assertIn('advertisement failed', msg)

    def test_port_closed(self):
        status, msg = ha._tang_health_from_probe(True, False, False, None, None)
        self.assertEqual(status, 'unhealthy')
        self.assertIn('port not accessible', msg)

    def test_not_running(self):
        status, msg = ha._tang_health_from_probe(False, False, False, None, None)
        self.assertEqual(status, 'unhealthy')
        self.assertIn('not running', msg)


class CheckTangServiceTests(unittest.TestCase):
    def test_error_returns_error_status(self):
        with mock.patch.object(ha, 'check_service_status',
                               side_effect=RuntimeError('boom')):
            result = ha.check_tang_service()
        self.assertEqual(result['status'], 'error')


if __name__ == '__main__':
    unittest.main()
