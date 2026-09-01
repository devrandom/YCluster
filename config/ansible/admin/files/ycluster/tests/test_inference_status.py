"""Tests for inference_manager status rendering helpers."""

import io
import unittest
from contextlib import redirect_stdout

from ycluster.utils import inference_manager as im


class RenderStatusTests(unittest.TestCase):
    def test_no_backends(self):
        out = io.StringIO()
        with redirect_stdout(out):
            im._render_status({'status': 'ok', 'message': 'none'}, False)
        self.assertIn('status: ok', out.getvalue())
        self.assertIn('none', out.getvalue())

    def test_full_render(self):
        data = {
            'status': 'ok', 'healthy': 1, 'down': 0, 'disabled': 0,
            'backends': [{'url': 'http://b:8000', 'state': 'healthy'}],
            'models': [{'name': 'm1', 'state': 'healthy', 'backends': ['a']}],
        }
        out = io.StringIO()
        with redirect_stdout(out):
            im._render_status(data, False)
        text = out.getvalue()
        self.assertIn('1 healthy / 0 down', text)
        self.assertIn('http://b:8000', text)
        self.assertIn('m1', text)

    def test_disabled_in_summary(self):
        data = {
            'status': 'ok', 'healthy': 1, 'down': 0, 'disabled': 2,
            'backends': [{'url': 'http://b:8000', 'state': 'healthy'}],
            'models': [],
        }
        out = io.StringIO()
        with redirect_stdout(out):
            im._render_status(data, False)
        self.assertIn('2 disabled', out.getvalue())


class BackendSectionTests(unittest.TestCase):
    def test_long_error_truncated(self):
        out = io.StringIO()
        with redirect_stdout(out):
            im._print_backend_section(
                [{'url': 'http://b', 'state': 'down', 'err': 'x' * 100}],
                lambda s: s)
        text = out.getvalue()
        self.assertIn('...', text)
        self.assertNotIn('x' * 100, text)


class ModelSectionTests(unittest.TestCase):
    def test_multi_backend_count(self):
        out = io.StringIO()
        with redirect_stdout(out):
            im._print_model_section(
                [{'name': 'm', 'state': 'healthy',
                  'backends': ['a', 'b']}], lambda s: s)
        self.assertIn('(2 backends)', out.getvalue())

    def test_empty_models_skipped(self):
        out = io.StringIO()
        with redirect_stdout(out):
            im._print_model_section([], lambda s: s)
        self.assertEqual(out.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
