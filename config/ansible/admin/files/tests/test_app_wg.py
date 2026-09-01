import unittest
from unittest import mock

import app_wg


class ValidateRegisterInputTests(unittest.TestCase):
    def test_valid_input(self):
        mac, node_type, pubkey, error = app_wg.validate_register_input(
            {'mac': 'mac', 'pubkey': 'A' * 43 + '='})
        self.assertEqual((mac, node_type), ('mac', 'compute'))
        self.assertIsNone(error)

    def test_missing_fields(self):
        _, _, _, error = app_wg.validate_register_input({})
        self.assertEqual(error, 'mac and pubkey are required')

    def test_bad_type(self):
        _, _, _, error = app_wg.validate_register_input(
            {'mac': 'm', 'pubkey': 'p', 'type': 'bad'})
        self.assertEqual(error, 'invalid type: bad')


class ValidPubkeyTests(unittest.TestCase):
    def test_length_and_encoding(self):
        self.assertTrue(app_wg.valid_pubkey('A' * 43 + '='))
        self.assertFalse(app_wg.valid_pubkey('short'))
        self.assertFalse(app_wg.valid_pubkey('!bad-base64!'))


class RegisterPeerTests(unittest.TestCase):
    def test_allocation_error(self):
        getter = mock.Mock(side_effect=RuntimeError('down'))
        _, _, failure = app_wg.register_peer(getter, None, 'm', 'compute', 'k')
        self.assertEqual(failure, (500, 'allocation failed: down'))

    def test_value_error(self):
        getter = mock.Mock(return_value={'hostname': 'h'})
        register = mock.Mock(side_effect=ValueError('bad'))
        _, _, failure = app_wg.register_peer(getter, register, 'm', 'compute', 'k')
        self.assertEqual(failure, (400, 'bad'))

    def test_success(self):
        getter = mock.Mock(return_value={'hostname': 'h'})
        register = mock.Mock(return_value={'status': 'pending'})
        allocation, peer, failure = app_wg.register_peer(
            getter, register, 'm', 'compute', 'k')
        self.assertEqual(allocation, {'hostname': 'h'})
        self.assertEqual(peer, {'status': 'pending'})
        self.assertIsNone(failure)


if __name__ == '__main__':
    unittest.main()


class RegisterFailureTests(unittest.TestCase):
    def test_registration_error(self):
        getter = mock.Mock(return_value={'hostname': 'h'})
        register = mock.Mock(side_effect=RuntimeError('boom'))
        _, _, failure = app_wg.register_peer(getter, register, 'm', 'compute', 'k')
        self.assertEqual(failure, (500, 'registration failed: boom'))
