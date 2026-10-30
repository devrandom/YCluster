import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cluster_exec import server


class ControlPathTests(unittest.TestCase):
    def _resolve(self, value: str) -> tuple[str | None, str]:
        stderr = io.StringIO()
        with patch.dict(os.environ, {"CE_SSH_CONTROL_PATH": value}, clear=False):
            with contextlib.redirect_stderr(stderr):
                result = server._control_path_from_env()
        return result, stderr.getvalue()

    def test_empty_disables_multiplexing(self):
        result, err = self._resolve("  ")
        self.assertIsNone(result)
        self.assertEqual(err, "")

    def test_private_directory_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected = os.path.abspath(os.path.join(tmp, "ce-%C"))
            result, err = self._resolve(expected)
        self.assertEqual(result, expected)
        self.assertEqual(err, "")

    def test_missing_parent_disables_multiplexing(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, err = self._resolve(os.path.join(tmp, "missing", "ce-%C"))
        self.assertIsNone(result)
        self.assertIn("disabling ssh multiplexing", err)

    def test_group_or_world_writable_parent_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "open"
            parent.mkdir(mode=0o777)
            os.chmod(parent, 0o777)
            result, err = self._resolve(str(parent / "ce-%C"))
        self.assertIsNone(result)
        self.assertIn("is group/world-writable", err)

    def test_overlong_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, err = self._resolve(os.path.join(tmp, "x" * 120, "ce-%C"))
        self.assertIsNone(result)
        self.assertIn("too long", err)


if __name__ == "__main__":
    unittest.main()
