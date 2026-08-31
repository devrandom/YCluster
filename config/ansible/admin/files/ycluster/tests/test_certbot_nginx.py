"""Tests for nginx config template updates."""

import contextlib
import io
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from ycluster.utils import certbot_manager as cm


class UpdateNginxConfigsTests(unittest.TestCase):
    def setUp(self):
        self.sites = [('app', Path('/etc/nginx/templates/app.conf.j2'))]

    def run_update(self, sites, test_rc=0, process_results=()):
        output = io.StringIO()
        test = subprocess.CompletedProcess([], test_rc, stdout='',
                                           stderr='bad config')
        with contextlib.redirect_stdout(output), \
                mock.patch.object(cm, '_primary_domain_and_tls',
                                  return_value=('example.test', True)), \
                mock.patch.object(cm, 'discover_nginx_templates',
                                  return_value=sites), \
                mock.patch.object(cm, '_process_nginx_site',
                                  side_effect=process_results) as process, \
                mock.patch.object(cm.subprocess, 'run', return_value=test) \
                as run:
            result = cm.update_nginx_configs(local_mode=False)
        return result, output.getvalue(), process, run

    def test_no_primary_domain(self):
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(cm, '_primary_domain_and_tls',
                                  return_value=(None, True)):
            self.assertFalse(cm.update_nginx_configs())

    def test_no_templates(self):
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(cm, '_primary_domain_and_tls',
                                  return_value=('example.test', True)), \
                mock.patch.object(cm, 'discover_nginx_templates',
                                  return_value=[]):
            self.assertFalse(cm.update_nginx_configs())

    def test_all_up_to_date(self):
        result, output, process, run = self.run_update(
            self.sites, process_results=[False])
        self.assertTrue(result)
        self.assertIn('All nginx configurations are already up to date',
                      output)
        run.assert_not_called()

    def test_update_runs_nginx_test(self):
        result, output, process, run = self.run_update(
            self.sites * 2, process_results=[True, False])
        self.assertTrue(result)
        self.assertEqual(process.call_count, 2)
        run.assert_called_once_with(['nginx', '-t'], capture_output=True,
                                    text=True)

    def test_nginx_test_failure(self):
        result, output, _, run = self.run_update(
            self.sites, test_rc=1, process_results=[True])
        self.assertFalse(result)
        self.assertIn('Nginx config test failed: bad config', output)


class ProcessNginxSiteTests(unittest.TestCase):
    def test_process_site_writes_rendered_config(self):
        with TemporaryDirectory() as temp:
            template = Path(temp) / 'app_site.conf.j2'
            template.write_text('domain={{ domain }}')
            output = io.StringIO()
            root = Path(temp)
            with contextlib.redirect_stdout(output), \
                    mock.patch.object(cm, 'Path', lambda p: root / Path(p).name):
                result = cm._process_nginx_site(
                    'app_site', template, 'example.test', True)
            self.assertTrue(result)
            self.assertIn('configuration updated', output.getvalue())

    def test_process_site_error_is_caught(self):
        template = Path('/nonexistent/app.conf.j2')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = cm._process_nginx_site('app', template, 'example.test', True)
        self.assertFalse(result)
        self.assertIn('error processing template', output.getvalue())


if __name__ == '__main__':
    unittest.main()
