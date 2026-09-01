"""Tests for https_config CLI rendering helpers."""

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from ycluster.utils import https_config as hc


class PrintConfigTests(unittest.TestCase):
    def test_no_config(self):
        with mock.patch.object(hc, 'get_https_config', return_value={}):
            out = io.StringIO()
            with redirect_stdout(out):
                hc._print_https_config()
        self.assertIn('No HTTPS configuration found', out.getvalue())

    def test_full_config(self):
        cfg = {'domain': 'x.example', 'aliases': ['a.example'],
               'email': 'a@x'}
        with mock.patch.object(hc, 'get_https_config', return_value=cfg), \
             mock.patch.object(hc, 'get_all_domains',
                               return_value=['x.example', 'a.example']):
            out = io.StringIO()
            with redirect_stdout(out):
                hc._print_https_config()
        text = out.getvalue()
        self.assertIn('Primary domain: x.example', text)
        self.assertIn('Aliases: a.example', text)
        self.assertIn('Email: a@x', text)
        self.assertIn('All domains: x.example, a.example', text)


class ListDomainsTests(unittest.TestCase):
    def test_domains(self):
        with mock.patch.object(hc, 'get_all_domains',
                               return_value=['a.example', 'b.example']):
            out = io.StringIO()
            with redirect_stdout(out):
                hc._list_domains()
        self.assertEqual(out.getvalue(), 'a.example\nb.example\n')

    def test_empty(self):
        with mock.patch.object(hc, 'get_all_domains', return_value=[]):
            out = io.StringIO()
            with redirect_stdout(out):
                hc._list_domains()
        self.assertIn('No domains configured', out.getvalue())


class ParserTests(unittest.TestCase):
    def test_commands_registered(self):
        parser = hc._build_parser()
        no_arg = {'get', 'list-domains', 'delete'}
        for cmd in ('set-domain', 'add-alias', 'remove-alias',
                    'set-email', 'get', 'list-domains', 'delete'):
            argv = [cmd] if cmd in no_arg else [cmd, 'x']
            args = parser.parse_args(argv)
            self.assertEqual(args.command, cmd)


if __name__ == '__main__':
    unittest.main()
