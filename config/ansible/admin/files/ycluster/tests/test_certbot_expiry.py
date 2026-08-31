import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from ycluster.utils import certbot_manager


class CertExpiryTests(unittest.TestCase):
    def test_prints_not_after(self):
        result = mock.Mock(returncode=0, stdout='foo\nNot After: tomorrow\nbar')
        out = io.StringIO()
        with redirect_stdout(out), \
                mock.patch.object(certbot_manager.subprocess, 'run',
                                  return_value=result):
            certbot_manager._print_expiry(b'cert')
        self.assertIn('Expires: Not After: tomorrow', out.getvalue())

    def test_swallows_openssl_errors(self):
        result = mock.Mock(returncode=1, stdout='')
        out = io.StringIO()
        with redirect_stdout(out), \
                mock.patch.object(certbot_manager.subprocess, 'run',
                                  return_value=result):
            certbot_manager._print_expiry(b'cert')
        self.assertEqual(out.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
