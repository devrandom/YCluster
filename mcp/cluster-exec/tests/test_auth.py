import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cluster_exec import auth


class DefaultTokenPathTests(unittest.TestCase):
    def test_default(self):
        with mock.patch.dict(os.environ, {"HOME": "/home/test"}, clear=True):
            self.assertEqual(
                auth.default_token_path(),
                Path("/home/test/.config/cluster-exec/bearer"),
            )

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"CE_BEARER_PATH": "/tmp/custom-bearer"}):
            self.assertEqual(auth.default_token_path(), Path("/tmp/custom-bearer"))

    def test_env_override_expands_tilde(self):
        with mock.patch.dict(os.environ, {"CE_BEARER_PATH": "~/foo/bearer", "HOME": "/home/test"}):
            self.assertEqual(
                auth.default_token_path(),
                Path("/home/test/foo/bearer"),
            )

    def test_env_override_empty_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"CE_BEARER_PATH": "", "HOME": "/home/test"}, clear=True):
            self.assertEqual(
                auth.default_token_path(),
                Path("/home/test/.config/cluster-exec/bearer"),
            )


class LoadOrCreateTokenTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmpdir, ignore_errors=True))
        self.path = self.tmpdir / "bearer"

    def test_creates_new_file(self):
        token = auth.load_or_create_token(self.path)
        self.assertTrue(self.path.exists())
        self.assertEqual(self.path.read_text().strip(), token)

    def test_creates_file_with_mode_0600(self):
        auth.load_or_create_token(self.path)
        mode = self.path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_existing_file_is_read_not_regenerated(self):
        self.path.write_text("preexisting-token\n")
        token = auth.load_or_create_token(self.path)
        self.assertEqual(token, "preexisting-token")

    def test_existing_file_strips_whitespace(self):
        self.path.write_text("  spaced-token  \n")
        token = auth.load_or_create_token(self.path)
        self.assertEqual(token, "spaced-token")

    def test_existing_file_mode_is_preserved(self):
        self.path.write_text("preexisting\n")
        os.chmod(self.path, 0o644)
        auth.load_or_create_token(self.path)
        mode = self.path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o644)

    def test_creates_parent_dirs(self):
        deep = self.tmpdir / "a" / "b" / "c" / "bearer"
        token = auth.load_or_create_token(deep)
        self.assertTrue(deep.exists())
        self.assertEqual(deep.read_text().strip(), token)
        mode = deep.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_generated_token_has_substantial_entropy(self):
        token = auth.load_or_create_token(self.path)
        self.assertGreaterEqual(len(token), 32)


class StaticTokenVerifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_correct_token_returns_access_token(self):
        v = auth.StaticTokenVerifier("secret-token")
        info = await v.verify_token("secret-token")
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.token, "secret-token")
        self.assertEqual(info.client_id, "cluster-exec-local")
        self.assertEqual(info.scopes, [])

    async def test_wrong_token_returns_none(self):
        v = auth.StaticTokenVerifier("secret-token")
        info = await v.verify_token("wrong-token")
        self.assertIsNone(info)

    async def test_empty_token_returns_none(self):
        v = auth.StaticTokenVerifier("secret-token")
        info = await v.verify_token("")
        self.assertIsNone(info)


if __name__ == "__main__":
    unittest.main()
