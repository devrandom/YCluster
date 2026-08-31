"""Tests for the HTTPS CLI output formatting."""

import contextlib
import io
import unittest
from unittest import mock

from ycluster.cli import https


class HttpsGetTests(unittest.TestCase):
    def render(self, config, domains):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), \
                mock.patch.object(https.https_config, 'get_https_config',
                                  return_value=config), \
                mock.patch.object(https.https_config, 'get_all_domains',
                                  return_value=domains):
            https.https_get(None)
        return output.getvalue()

    def test_full_config(self):
        rendered = self.render(
            {'domain': 'example.test', 'aliases': ['a.test', 'b.test'],
             'email': 'user@example.test'}, ['example.test', 'a.test'])
        self.assertIn('HTTPS Configuration:', rendered)
        self.assertIn('Primary domain: example.test', rendered)
        self.assertIn('Aliases: a.test, b.test', rendered)
        self.assertIn('Email: user@example.test', rendered)
        self.assertIn('All domains: example.test, a.test', rendered)

    def test_minimal_config(self):
        rendered = self.render({'aliases': []}, [])
        self.assertIn('HTTPS Configuration:', rendered)
        self.assertNotIn('Primary domain:', rendered)
        self.assertNotIn('Aliases:', rendered)
        self.assertNotIn('Email:', rendered)
        self.assertNotIn('All domains:', rendered)

    def test_missing_config(self):
        self.assertEqual(self.render(None, []), 'No HTTPS configuration found\n')


if __name__ == '__main__':
    unittest.main()
