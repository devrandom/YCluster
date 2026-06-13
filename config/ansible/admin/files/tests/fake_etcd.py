"""In-memory stand-in for the etcd3 client surface the DAL uses.

vm_manager's data-access layer (and thus the admin web app, which now goes
through it) only ever calls get / get_prefix / put / delete on the client
returned by get_etcd_client(). Patching get_etcd_client to return one of
these gives deterministic, server-free tests — no etcd3 server, and the
etcd3 driver itself is never exercised.
"""

import json


class _Meta:
    __slots__ = ("key",)

    def __init__(self, key):
        self.key = key.encode() if isinstance(key, str) else key


class FakeEtcd:
    def __init__(self, data=None):
        self.store = dict(data or {})            # str key -> str value

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

    # --- test conveniences ---
    def seed_json(self, key, obj):
        self.store[key] = json.dumps(obj)
        return self

    def get_json(self, key):
        v = self.store.get(key)
        return json.loads(v) if v is not None else None
