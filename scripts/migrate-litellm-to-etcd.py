#!/usr/bin/env python3
"""
One-time migration: export LiteLLM's model list to etcd for local-ai-proxy.

Run on a core node (storage leader recommended — etcd writes go via the
local node). Reads master key from etcd, calls LiteLLM's
/v1/model/info with it, and writes each (model_name -> api_base) under
/cluster/config/inference/models/<name> as JSON
{"backends": [{"api_base": "..."}, ...]}.

Usage:
    scripts/migrate-litellm-to-etcd.py [--dry-run] [--prefix <path>] \\
        [--litellm-url http://localhost:4000]

If multiple LiteLLM rows share a model_name (fan-out), all of their
api_bases are collected into the backends list in the order they
appear. The local-ai-proxy router currently picks backends[0]; the
list is stored so future fan-out serving needs no etcd migration.
"""

import argparse
import json
import sys
from urllib.parse import urlparse, urlunparse

import etcd3
import requests


DEFAULT_PREFIX = "/cluster/config/inference/models/"
DEFAULT_LITELLM_URL = "http://localhost:4000"
MASTER_KEY_ETCD_PATH = "/cluster/config/litellm/master-key"


def fetch_master_key(client):
    value, _ = client.get(MASTER_KEY_ETCD_PATH)
    if value is None:
        sys.exit(f"error: master key not found at etcd {MASTER_KEY_ETCD_PATH}")
    return value.decode().strip()


def fetch_litellm_models(base_url, master_key):
    url = f"{base_url.rstrip('/')}/v1/model/info"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {master_key}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def normalize_api_base(url):
    """Strip trailing '/v1' from an api_base, since local-ai-proxy forwards
    the client's full path (including /v1/...)."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    return urlunparse(parsed._replace(path=path))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be written without touching etcd",
    )
    parser.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        help=f"etcd key prefix for model entries (default: {DEFAULT_PREFIX})",
    )
    parser.add_argument(
        "--litellm-url",
        default=DEFAULT_LITELLM_URL,
        help=f"LiteLLM base URL (default: {DEFAULT_LITELLM_URL})",
    )
    args = parser.parse_args()

    if not args.prefix.endswith("/"):
        args.prefix += "/"

    client = etcd3.client()
    master_key = fetch_master_key(client)
    models = fetch_litellm_models(args.litellm_url, master_key)

    print(f"LiteLLM reports {len(models)} model entries", file=sys.stderr)

    # Group rows by model_name, preserving order of api_bases.
    grouped = {}  # model_name -> list[api_base]
    order = []    # preserves first-seen order of model names
    skipped = 0
    for m in models:
        name = m.get("model_name")
        params = m.get("litellm_params") or {}
        api_base = params.get("api_base")
        if not name or not api_base:
            print(f"  skip: missing model_name or api_base in row: {m}", file=sys.stderr)
            skipped += 1
            continue
        if name not in grouped:
            grouped[name] = []
            order.append(name)
        grouped[name].append(normalize_api_base(api_base))

    wrote = 0
    for name in order:
        bases = grouped[name]
        key = f"{args.prefix}{name}"
        value = json.dumps(
            {"backends": [{"api_base": b} for b in bases]},
            separators=(",", ":"),
        )
        if args.dry_run:
            print(f"  DRY PUT {key} -> {value}")
        else:
            client.put(key, value)
            print(f"  PUT {key} -> {value}")
        wrote += 1
        if len(bases) > 1:
            print(
                f"  note: {name!r} has {len(bases)} backends; "
                "local-ai-proxy uses the first until fan-out lands",
                file=sys.stderr,
            )

    verb = "would write" if args.dry_run else "wrote"
    print(f"done: {verb} {wrote} models; skipped {skipped} malformed rows", file=sys.stderr)


if __name__ == "__main__":
    main()
