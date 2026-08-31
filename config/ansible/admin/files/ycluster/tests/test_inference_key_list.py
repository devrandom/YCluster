import io
import unittest
from contextlib import redirect_stdout

from ycluster.cli import inference


class KeyTableTests(unittest.TestCase):
    def test_prints_records_and_empty_owner(self):
        out = io.StringIO()
        data = {
            'b': [{'key_id': 'k2', 'created_at': '2026-01-02T03', 'label': 'beta'}],
            'a': [],
        }
        with redirect_stdout(out):
            inference._print_key_table(data)
        self.assertIn('KEY ID', out.getvalue())
        self.assertIn('a', out.getvalue())
        self.assertIn('(no keys)', out.getvalue())
        self.assertIn('k2', out.getvalue())


if __name__ == '__main__':
    unittest.main()
