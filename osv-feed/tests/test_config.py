import os
import shutil
import tempfile
import unittest
from unittest import mock

from osv_feed import config


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="osv-config-")
        self.system = os.path.join(self.tmp, "system-config.toml")
        self.user = os.path.join(self.tmp, "user-config.toml")
        env = dict(os.environ)
        env.pop("OSV_FEED_DB_DIR", None)
        env.pop("OSV_FEED_ACK_DB", None)
        env["OSV_FEED_SYSTEM_CONFIG"] = self.system
        env["OSV_FEED_USER_CONFIG"] = self.user
        self.env_patch = mock.patch.dict(os.environ, env, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _write(self, path, body):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)

    def test_default_db_dir(self):
        self.assertEqual(
            config.resolve_db_dir(None),
            os.path.expanduser("~/.cache/osv-feed"),
        )

    def test_default_ack_db(self):
        self.assertEqual(
            config.resolve_ack_db(None),
            os.path.expanduser("~/.cache/osv-feed-acks.json"),
        )

    def test_system_config(self):
        self._write(self.system, '[feed]\ndb_dir = "/var/lib/osv-feed"\n')
        self.assertEqual(config.resolve_db_dir(None), "/var/lib/osv-feed")

    def test_user_config_overrides_system(self):
        self._write(self.system, '[feed]\ndb_dir = "/var/lib/osv-feed"\n')
        self._write(self.user, '[feed]\ndb_dir = "/home/me/cache/osv-feed"\n')
        self.assertEqual(config.resolve_db_dir(None), "/home/me/cache/osv-feed")

    def test_env_wins_over_config(self):
        self._write(self.system, '[feed]\ndb_dir = "/var/lib/osv-feed"\n')
        with mock.patch.dict(os.environ, {"OSV_FEED_DB_DIR": "/data/osv-feed"}, clear=False):
            self.assertEqual(config.resolve_db_dir(None), "/data/osv-feed")

    def test_cli_wins_over_everything(self):
        self._write(self.system, '[feed]\ndb_dir = "/var/lib/osv-feed"\n')
        with mock.patch.dict(os.environ, {"OSV_FEED_DB_DIR": "/data/osv-feed"}, clear=False):
            self.assertEqual(config.resolve_db_dir("/cli/db"), "/cli/db")

    def test_ack_db_from_config(self):
        self._write(self.system, '[feed]\nack_db = "/srv/osv-feed/acks.json"\n')
        self.assertEqual(config.resolve_ack_db(None), "/srv/osv-feed/acks.json")

    def test_ack_db_env_wins_over_config(self):
        self._write(self.user, '[feed]\nack_db = "/srv/osv-feed/acks.json"\n')
        with mock.patch.dict(os.environ, {"OSV_FEED_ACK_DB": "/tmp/acks.json"}, clear=False):
            self.assertEqual(config.resolve_ack_db(None), "/tmp/acks.json")

    def test_ack_db_cli_wins_over_everything(self):
        self._write(self.user, '[feed]\nack_db = "/srv/osv-feed/acks.json"\n')
        with mock.patch.dict(os.environ, {"OSV_FEED_ACK_DB": "/tmp/acks.json"}, clear=False):
            self.assertEqual(config.resolve_ack_db("/cli/acks.json"), "/cli/acks.json")

    def test_tracking_categories_loaded(self):
        self._write(
            self.system,
            "[tracking.perimeter]\n"
            'packages = ["openssh-client", "nginx"]\n'
            'severity = "high"\n'
            'interests = ["rce", "remote code execution"]\n',
        )
        self._write(
            self.user,
            "[tracking.user]\n"
            'packages = ["sudo"]\n'
            'interests = ["privilege escalation"]\n',
        )
        categories = config.tracking_categories()
        self.assertEqual(
            {"openssh-client", "nginx"}, set(categories["perimeter"]["packages"])
        )
        self.assertEqual("high", categories["perimeter"]["severity"])
        self.assertEqual(["sudo"], categories["user"]["packages"])

    def test_category_config_default(self):
        self.assertEqual(
            ["openssh", "nginx"],
            config.category_config("perimeter")["packages"],
        )
        self.assertEqual(
            ["sudo"],
            config.category_config("user")["packages"],
        )

    def test_category_config_unknown_returns_none(self):
        self.assertIsNone(config.category_config("does_not_exist"))

    def test_category_config_user_overrides_default(self):
        self._write(
            self.system,
            "[tracking.perimeter]\n"
            'packages = ["haproxy"]\n'
            'severity = "critical"\n',
        )
        cfg = config.category_config("perimeter")
        self.assertEqual(["haproxy"], cfg["packages"])
        self.assertEqual("critical", cfg["severity"])

    def test_tracking_categories_includes_defaults_and_overrides(self):
        self._write(
            self.user,
            "[tracking.perimeter]\n"
            'packages = ["haproxy"]\n'
            'severity = "critical"\n',
        )
        self._write(
            self.system,
            "[tracking.custom]\n"
            'packages = ["vim"]\n'
        )
        categories = config.tracking_categories()
        self.assertEqual(["haproxy"], categories["perimeter"]["packages"])
        self.assertEqual(["vim"], categories["custom"]["packages"])
        self.assertIn("network", categories)
        self.assertIn("runtimes", categories)

    def test_categories_for_package_default(self):
        self.assertEqual(["perimeter"], config.categories_for_package("openssh"))
        self.assertEqual(["user"], config.categories_for_package("sudo"))
        self.assertEqual([], config.categories_for_package("does_not_exist"))

    def test_categories_for_package_includes_config(self):
        self._write(self.system, "[tracking.edge]\npackages = ['haproxy']\n")
        self.assertEqual(["edge"], config.categories_for_package("haproxy"))
        self.assertEqual(["edge"], config.categories_for_package("haproxy", {"tracking": {"edge": {"packages": ["haproxy"]}}}))

    def test_invalid_toml_raises(self):
        self._write(self.system, "not [valid toml\n")
        with self.assertRaises(ValueError):
            config.resolve_db_dir(None)


if __name__ == "__main__":
    unittest.main()
