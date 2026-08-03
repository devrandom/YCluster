"""Unit tests for the local-ai-proxy auth validator (local-ai-proxy-auth.py).

Loads the validator by file path (it lives under app/files, outside the
ycluster package) with a dict-backed fake etcd so nothing touches etcd or
a socket.

Run from config/ansible/admin/files:
    PYTHONPATH=.:ycluster python3 -m unittest discover -s tests
(via ./run-tests.sh at the repo root).
"""

import importlib.util
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_FILES = os.path.dirname(_HERE)
for p in (_HERE, _FILES, os.path.join(_FILES, "ycluster")):
    if p not in sys.path:
        sys.path.insert(0, p)

from argon2 import PasswordHasher                      # noqa: E402
from fake_etcd import FakeEtcd                         # noqa: E402

import ycluster.common.apikeys as ak                   # noqa: E402

_VALIDATOR = os.path.normpath(
    os.path.join(_FILES, "..", "..", "app", "files", "local-ai-proxy-auth.py"))


def _load_validator():
    spec = importlib.util.spec_from_file_location("local_ai_proxy_auth", _VALIDATOR)
    assert spec is not None and spec.loader is not None
    v = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(v)
    return v


def _fast_hasher():
    return PasswordHasher(time_cost=1, memory_cost=1024, hash_len=16, salt_len=8)


class ValidatorTestBase(unittest.TestCase):
    def setUp(self):
        self.validator = _load_validator()
        self.etcd = FakeEtcd()
        self.hasher = _fast_hasher()

        self._ak_orig = ak.get_etcd_client
        self._v_orig = self.validator.get_etcd_client
        ak.get_etcd_client = lambda: self.etcd
        self.validator.get_etcd_client = lambda: self.etcd

        self.validator.reset_store()
        self.validator._state["master_key"] = None

    def tearDown(self):
        ak.get_etcd_client = self._ak_orig
        self.validator.get_etcd_client = self._v_orig

    # --- seeding ---
    def seed_key(self, email, token, groups=None, key_id="k1"):
        rec = {"hash": self.hasher.hash(token), "created_at": "t", "label": ""}
        self.etcd.seed_json(ak.USERS_PREFIX + email + ak.KEYS_SUFFIX + key_id, rec)
        if groups is not None:
            self.etcd.seed_json(ak.USERS_PREFIX + email + ak.GROUPS_SUFFIX, groups)

    def seed_master(self, value="cluster-master"):
        self.etcd.store[self.validator.MASTER_KEY_ETCD_PATH] = value
        self.validator._state["master_key"] = None

    def auth(self, token):
        return self.validator.app.test_client().get(
            "/auth", headers={"Authorization": "Bearer " + token})


class UserStoreTests(ValidatorTestBase):
    def test_auth_user_matches_by_hash(self):
        self.seed_key("alice@x", "sk-abc", groups=["g1", "admin"])
        store = self.validator.UserStore(self.etcd, hasher=self.hasher)
        self.assertEqual(store.auth_user("sk-abc"), ("alice@x", ["g1", "admin"]))

    def test_auth_user_rejects_bad_and_non_sk(self):
        self.seed_key("alice@x", "sk-abc")
        store = self.validator.UserStore(self.etcd, hasher=self.hasher)
        self.assertIsNone(store.auth_user("sk-bogus"))
        self.assertIsNone(store.auth_user("not-a-key"))
        self.assertIsNone(store.auth_user(""))

    def test_auth_user_malformed_stored_hash(self):
        # A stored hash that argon2 can't parse raises InvalidHash (both the
        # apt python3-argon2 and newer pip surface this name); the store must
        # treat it as "no match", never propagate.
        rec = {"hash": "not-an-argon2-hash", "created_at": "t", "label": ""}
        self.etcd.seed_json(ak.USERS_PREFIX + "alice@x" + ak.KEYS_SUFFIX + "k1", rec)
        store = self.validator.UserStore(self.etcd, hasher=self.hasher)
        self.assertIsNone(store.auth_user("sk-anything"))

    def test_auth_user_empty_store(self):
        store = self.validator.UserStore(self.etcd, hasher=self.hasher)
        self.assertIsNone(store.auth_user("sk-anything"))


class AuthRouteTests(ValidatorTestBase):
    def test_master_key_returns_root(self):
        self.seed_master("cluster-master")
        resp = self.auth("cluster-master")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["X-User-Id"], "root")
        self.assertEqual(resp.headers["X-User-Groups"], "")

    def test_per_user_key_returns_email_and_groups(self):
        self.seed_key("alice@x", "sk-alice", groups=["g1", "admin"])
        self.validator.reset_store(
            self.validator.UserStore(self.etcd, hasher=self.hasher))
        resp = self.auth("sk-alice")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["X-User-Id"], "alice@x")
        self.assertEqual(resp.headers["X-User-Groups"], "g1,admin")

    def test_unknown_key_401(self):
        self.seed_key("alice@x", "sk-alice")
        self.validator.reset_store(
            self.validator.UserStore(self.etcd, hasher=self.hasher))
        resp = self.auth("sk-unknown")
        self.assertEqual(resp.status_code, 401)

    def test_no_auth_header_401(self):
        self.validator.reset_store(
            self.validator.UserStore(self.etcd, hasher=self.hasher))
        resp = self.validator.app.test_client().get("/auth")
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
