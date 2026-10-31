"""Tests for the admin database connection helper (app_project_db.py)."""

import types
import unittest
from unittest import mock

import app_project_db
from ycluster.common import etcd_utils


def fake_psycopg2(connect):
    module = types.ModuleType("psycopg2")
    module.connect = connect
    return module


class ConnectTests(unittest.TestCase):
    def test_connect_passes_credentials_from_etcd(self):
        connect = mock.Mock(return_value="connection")
        client = mock.Mock()
        client.get.return_value = (b"secret", None)
        with mock.patch.dict("sys.modules",
                             {"psycopg2": fake_psycopg2(connect)}), \
                mock.patch.object(etcd_utils, "get_etcd_client",
                                  return_value=client):
            self.assertEqual(app_project_db.connect(), "connection")
        connect.assert_called_once_with(
            host=app_project_db.STORAGE_VIP,
            database=app_project_db.DATABASE,
            user=app_project_db.ROLE,
            password="secret",
        )

    def test_connect_accepts_host_override_and_text_password(self):
        connect = mock.Mock(return_value="connection")
        client = mock.Mock()
        client.get.return_value = ("text-secret", None)
        with mock.patch.dict("sys.modules",
                             {"psycopg2": fake_psycopg2(connect)}), \
                mock.patch.object(etcd_utils, "get_etcd_client",
                                  return_value=client), \
                mock.patch.dict("os.environ", {"ADMIN_DB_HOST": "db.test"}):
            self.assertEqual(app_project_db.connect(), "connection")
        connect.assert_called_once_with(
            host="db.test",
            database=app_project_db.DATABASE,
            user=app_project_db.ROLE,
            password="text-secret",
        )

    def test_connect_requires_password(self):
        client = mock.Mock()
        client.get.return_value = (None, None)
        with mock.patch.dict("sys.modules",
                             {"psycopg2": fake_psycopg2(mock.Mock())}), \
                mock.patch.object(etcd_utils, "get_etcd_client",
                                  return_value=client):
            with self.assertRaises(RuntimeError):
                app_project_db.connect()


if __name__ == "__main__":
    unittest.main()
