import io
import unittest
from contextlib import redirect_stdout

from ycluster.cli import backup


class RecipientsParsingTests(unittest.TestCase):
    def test_parse_and_print(self):
        value = b'{"public_key": "age1", "created_at": "now", "description": "d"}'
        meta = type('Meta', (), {'key': b'/cluster/backup/recipients/name'})
        got = backup._parse_recipients([(value, meta)])
        self.assertEqual(got, [{'name': 'name', 'public_key': 'age1',
                                'created_at': 'now', 'description': 'd'}])

    def test_bad_json_skipped(self):
        meta = type('Meta', (), {'key': b'/bad'})
        out = io.StringIO()
        with redirect_stdout(out):
            got = backup._parse_recipients([(b'bad', meta)])
        self.assertEqual(got, [])
        self.assertIn('Error parsing', out.getvalue())

    def test_print_empty(self):
        out = io.StringIO()
        with redirect_stdout(out):
            backup._print_recipients([])
        self.assertIn('No backup', out.getvalue())


if __name__ == '__main__':
    unittest.main()
