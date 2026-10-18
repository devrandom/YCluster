"""Unit tests for the gateway API-key DAL (ycluster.common.apikeys).

Uses a dict-backed fake etcd patched over the DAL's get_etcd_client seam.
Keys are hashed with real argon2id at low cost (still argon2id, per-key
salt — just fast enough for many records in a test run).
"""

import json
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.join(_HERE, "..", "ycluster")
for p in [_HERE, os.path.dirname(_HERE), _PKG]:
    if p not in sys.path:
        sys.path.insert(0, p)

from argon2 import PasswordHasher

import ycluster.common.apikeys as ak

FAST = dict(time_cost=1, memory_cost=1024, hash_len=16, salt_len=8)


class _Meta:
    __slots__ = ("key",)

    def __init__(self, key):
        self.key = key.encode() if isinstance(key, str) else key


class FakeEtcd:
    def __init__(self, data=None):
        self.store = dict(data or {})

    def get(self, key):
        v = self.store.get(key)
        return (v.encode() if v is not None else None, _Meta(key))

    def get_prefix(self, prefix):
        return [(v.encode(), _Meta(k))
                for k, v in sorted(self.store.items())
                if k.startswith(prefix)]

    def put(self, key, value):
        self.store[key] = (value.decode()
                           if isinstance(value, (bytes, bytearray)) else value)

    def delete(self, key):
        self.store.pop(key, None)

    def seed_json(self, key, obj):
        self.store[key] = json.dumps(obj)
        return self

    def get_json(self, key):
        v = self.store.get(key)
        return json.loads(v) if v is not None else None


def _fake_psycopg2(rows):
    """A fake psycopg2 module whose connect() yields the given rows."""

    class Cursor:
        def __init__(self):
            self._rows = rows

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql):
            pass

        def fetchall(self):
            return self._rows

    class Conn:
        autocommit = False

        def cursor(self):
            return Cursor()

        def close(self):
            pass

    class FakePsycopg2:
        def connect(self, dsn):
            return Conn()

    return FakePsycopg2()


class ApiKeysTestBase(unittest.TestCase):
    def setUp(self):
        self.etcd = FakeEtcd()
        self._orig = ak.get_etcd_client
        ak.get_etcd_client = lambda: self.etcd
        self.hasher = PasswordHasher(**FAST)

    def tearDown(self):
        ak.get_etcd_client = self._orig


class CreateListRevokeTests(ApiKeysTestBase):
    def test_create_returns_sk_prefix_and_stores_hash(self):
        secret = ak.create_key("alice@x", groups=["g1", "admin"], label="laptop",
                               hasher=self.hasher)
        self.assertTrue(secret.startswith("sk-"))
        # Only the hash is stored, never the plaintext.
        self.assertNotIn(secret, self.etcd.store.values())
        recs = ak.list_keys("alice@x")["alice@x"]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["label"], "laptop")
        groups = json.loads(self.etcd.store[ak.USERS_PREFIX + "alice@x" + ak.GROUPS_SUFFIX])
        self.assertEqual(groups, ["g1", "admin"])
        self.assertEqual(ak.validate_key(secret, hasher=self.hasher),
                         ("alice@x", ["g1", "admin"]))

    def test_list_never_returns_secret_or_hash(self):
        secret = ak.create_key("alice@x", hasher=self.hasher)
        for r in ak.list_keys("alice@x")["alice@x"]:
            self.assertNotIn("hash", r)
            self.assertNotIn("secret", r)
            self.assertNotIn(secret, str(r))

    def test_validate_rejects_bad_and_non_sk(self):
        ak.create_key("alice@x", hasher=self.hasher)
        self.assertIsNone(ak.validate_key("sk-bogus", hasher=self.hasher))
        self.assertIsNone(ak.validate_key("not-a-key", hasher=self.hasher))
        self.assertIsNone(ak.validate_key("", hasher=self.hasher))

    def test_validate_skips_invalid_hash_record(self):
        # A corrupted hash record must not blow up the whole scan.
        ak.create_key("alice@x", hasher=self.hasher)
        ak._put(ak.USERS_PREFIX + "bob@x" + ak.KEYS_SUFFIX + "deadbeef",
                {"hash": "not-a-valid-argon2-hash"})
        self.assertIsNone(ak.validate_key("sk-bogus", hasher=self.hasher))

    def test_revoke_is_idempotent(self):
        secret = ak.create_key("alice@x", hasher=self.hasher)
        key_id = ak.list_keys("alice@x")["alice@x"][0]["key_id"]
        self.assertTrue(ak.revoke_key(key_id))
        self.assertFalse(ak.revoke_key(key_id))          # already gone
        self.assertIsNone(ak.validate_key(secret, hasher=self.hasher))

    def test_revoke_across_owners(self):
        ak.create_key("alice@x", hasher=self.hasher)
        ak.create_key("bob@x", hasher=self.hasher)
        bob_key = ak.list_keys("bob@x")["bob@x"][0]["key_id"]
        self.assertTrue(ak.revoke_key(bob_key))
        self.assertEqual(ak.list_keys("bob@x")["bob@x"], [])
        self.assertEqual(len(ak.list_keys("alice@x")["alice@x"]), 1)

    def test_revoke_rejects_bad_id(self):
        with self.assertRaises(ValueError):
            ak.revoke_key("../../bad")


class DigestIndexTests(ApiKeysTestBase):
    def _key_path(self, email, key_id="k1"):
        return ak.USERS_PREFIX + email + ak.KEYS_SUFFIX + key_id

    def test_create_stores_digest_index(self):
        secret = ak.create_key("alice@x", hasher=self.hasher)
        key_id = ak.list_keys("alice@x")["alice@x"][0]["key_id"]
        rec = self.etcd.get_json(self._key_path("alice@x", key_id))
        self.assertEqual(rec["digest"], ak.token_digest(secret))
        self.assertNotIn(secret, self.etcd.store.values())
        self.assertEqual(ak.validate_key(secret, hasher=self.hasher),
                         ("alice@x", []))

    def test_legacy_record_validates_and_backfills(self):
        # Pre-digest record: validate_key must fall back to the argon2
        # scan, succeed, and write the digest back to etcd.
        token = "sk-legacy"
        self.etcd.seed_json(self._key_path("alice@x"), {
            "hash": self.hasher.hash(token),
            "created_at": "t",
            "label": "",
        })
        self.assertEqual(ak.validate_key(token, hasher=self.hasher),
                         ("alice@x", []))
        rec = self.etcd.get_json(self._key_path("alice@x"))
        self.assertEqual(rec["digest"], ak.token_digest(token))
        # Still validates afterwards (digest path now).
        self.assertEqual(ak.validate_key(token, hasher=self.hasher),
                         ("alice@x", []))

    def test_digest_mismatch_falls_through_to_legacy_scan(self):
        # A digest record that doesn't match the presented token must not
        # shadow a legacy record for the same owner that does.
        ak.create_key("alice@x", hasher=self.hasher)
        token = "sk-legacy"
        self.etcd.seed_json(self._key_path("alice@x", "legacy"), {
            "hash": self.hasher.hash(token),
            "created_at": "t",
            "label": "",
        })
        self.assertEqual(ak.validate_key(token, hasher=self.hasher),
                         ("alice@x", []))


class MigrateTests(ApiKeysTestBase):
    def test_migrate_copies_and_hashes_same_token(self):
        existing = "sk-existingtoken"
        rows = [(existing, "alice@x", "user", ["g1"])]
        count = self._run_migrate(rows)
        self.assertEqual(count, 1)
        # Same token keeps working — hashed, not re-issued, no rotation.
        self.assertEqual(ak.validate_key(existing, hasher=self.hasher),
                         ("alice@x", ["g1"]))
        # The plaintext is not in etcd.
        self.assertNotIn(existing, self.etcd.store.values())

    def test_migrate_appends_admin_role_group(self):
        rows = [("sk-admin", "root@x", "admin", [])]
        self._run_migrate(rows)
        self.assertEqual(ak.validate_key("sk-admin", hasher=self.hasher),
                         ("root@x", ["admin"]))

    def test_migrate_counts_rows(self):
        rows = [("sk-1", "a@x", "user", []), ("sk-2", "b@x", "user", [])]
        self.assertEqual(self._run_migrate(rows), 2)

    def test_migrate_is_idempotent(self):
        rows = [("sk-same", "alice@x", "user", ["g1"])]
        self.assertEqual(self._run_migrate(rows), 1)
        # Re-running does not mint a duplicate key-id for the same token.
        self.assertEqual(self._run_migrate(rows), 0)
        recs = ak.list_keys("alice@x")["alice@x"]
        self.assertEqual(len(recs), 1)
        # The original token still authenticates.
        self.assertEqual(ak.validate_key("sk-same", hasher=self.hasher),
                         ("alice@x", ["g1"]))

    def test_migrate_backfills_partial_run(self):
        rows = [("sk-a", "a@x", "user", []), ("sk-b", "b@x", "user", [])]
        self.assertEqual(self._run_migrate(rows), 2)
        # On a re-run, only rows that still lack a migrated record are copied.
        rows2 = [("sk-a", "a@x", "user", []), ("sk-b", "b@x", "user", []),
                 ("sk-c", "c@x", "user", [])]
        self.assertEqual(self._run_migrate(rows2), 1)
        self.assertEqual(len(ak.list_keys("a@x")["a@x"]), 1)
        self.assertEqual(len(ak.list_keys("b@x")["b@x"]), 1)
        self.assertEqual(len(ak.list_keys("c@x")["c@x"]), 1)

    def _run_migrate(self, rows):
        orig = ak._import_psycopg2
        try:
            ak._import_psycopg2 = lambda: _fake_psycopg2(rows)
            return ak.migrate_owui_keys(hasher=self.hasher)
        finally:
            ak._import_psycopg2 = orig


if __name__ == "__main__":
    unittest.main()
