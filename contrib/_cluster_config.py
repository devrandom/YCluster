"""Tiny helper: locate config.yml in the repo and return its values.

config.yml lives at the repo root and is gitignored. Schema:

    endpoint: https://your-cluster.example/
    api_token_file: host.token        # path relative to config.yml

Used by contrib/ scripts so the cluster endpoint isn't hardcoded.

When run on a cluster node (bastion or any host with `/etc/ansible/env.sh`):
  - endpoint defaults to http://inference.xc (the storage VIP, hits the
    local-ai-proxy directly — no TLS, no nginx auth_request roundtrip).
    config.yml's endpoint is ignored in this case.
  - token falls back to the cluster master key in etcd
    (`/cluster/config/inference/master-key`) if the api_token_file is
    absent. The master key is the cluster admin bearer; individual user
    keys (api_key rows in OWUI's postgres) still work too if you set
    api_token_file to one of them.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml


_BASTION_MARKER = Path("/etc/ansible/env.sh")
_ETCD_KEY = "/cluster/config/inference/master-key"
_BASTION_ENDPOINT = "http://inference.xc"


@dataclass(frozen=True)
class ClusterConfig:
    endpoint: str          # e.g. "http://inference.xc" (no trailing slash, no /v1/)
    token: str             # bearer token


def _is_on_bastion() -> bool:
    return _BASTION_MARKER.is_file()


def _find_config(start: Path) -> Path:
    """Walk up from start until config.yml is found, else raise."""
    for d in (start, *start.parents):
        candidate = d / "config.yml"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"no config.yml found at or above {start}; "
        "create one with `endpoint:` and `api_token_file:`"
    )


def _read_etcd_token() -> str | None:
    """Read the cluster master bearer key from etcd (only works on cluster nodes)."""
    if shutil.which("etcdctl") is None:
        return None
    try:
        out = subprocess.run(
            ["etcdctl", "get", _ETCD_KEY],
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    # etcdctl prints "<key>\n<value>\n"; the last line is the value.
    lines = out.splitlines()
    return lines[-1] if len(lines) >= 2 else None


def load(start: Path | None = None) -> ClusterConfig:
    on_bastion = _is_on_bastion()
    path = _find_config((start or Path(__file__)).resolve())
    cfg = yaml.safe_load(path.read_text()) or {}

    # Endpoint: on the bastion, always use the local proxy; otherwise honor
    # config.yml. The contrib/ scripts append "/v1/..." to whatever we return.
    if on_bastion:
        endpoint = _BASTION_ENDPOINT
    else:
        endpoint = (cfg.get("endpoint") or "").rstrip("/")

    # Token: file first (current dev-machine workflow), etcd fallback
    # (works on any cluster node, including the bastion). Either source
    # is fine; the master key is a cluster admin bearer.
    token = None
    token_file = cfg.get("api_token_file")
    if token_file:
        token_path = path.parent / token_file
        if token_path.is_file():
            token = token_path.read_text().strip()
    if not token:
        token = _read_etcd_token()

    if not endpoint:
        raise ValueError(
            f"{path}: `endpoint:` is required when not running on the bastion"
        )
    if not token:
        raise ValueError(
            f"no bearer token found (set `api_token_file:` in {path}, "
            f"or run on a cluster node with etcd access to {_ETCD_KEY})"
        )
    return ClusterConfig(endpoint=endpoint, token=token)
