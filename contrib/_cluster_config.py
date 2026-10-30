"""Cluster endpoint/token lookup plus inference model-registry reads.

Locates config.yml in the repo and returns its values. On cluster nodes it
also reads the gateway's etcd model registry (see `inference_backends`).

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

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml


_BASTION_MARKER = Path("/etc/ansible/env.sh")
_ETCD_KEY = "/cluster/config/inference/master-key"
_BASTION_ENDPOINT = "http://inference.xc"
_ETCD_CLIENT_ENV = Path("/etc/ycluster/etcd-client.env")
_ETCD_MODELS_PREFIX = "/cluster/config/inference/models/"


class EtcdUnavailable(RuntimeError):
    """The cluster etcd model registry cannot be read from this host."""


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
    config_path = None
    cfg = {}
    try:
        config_path = _find_config((start or Path(__file__)).resolve())
        cfg = yaml.safe_load(config_path.read_text()) or {}
    except FileNotFoundError:
        # The installed CLI lives under /opt/ycluster-inference with no
        # config.yml above it; on cluster nodes that is fine because the
        # endpoint/token come from the marker + etcd.
        if not on_bastion:
            raise

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
    if token_file and config_path is not None:
        token_path = config_path.parent / token_file
        if token_path.is_file():
            token = token_path.read_text().strip()
    if not token:
        token = _read_etcd_token()

    if not endpoint:
        raise ValueError(
            f"{config_path or 'config.yml'}: `endpoint:` is required when "
            "not running on the bastion"
        )
    if not token:
        raise ValueError(
            f"no bearer token found (set `api_token_file:` in "
            f"{config_path or 'config.yml'}, or run on a cluster node with "
            f"etcd access to {_ETCD_KEY})"
        )
    return ClusterConfig(endpoint=endpoint, token=token)


def _etcd_settings() -> dict[str, str]:
    """Return ETCD_* settings from the managed client env, live env winning."""
    values: dict[str, str] = {}
    try:
        for line in _ETCD_CLIENT_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    except OSError:
        pass
    values.update({k: v for k, v in os.environ.items() if k.startswith("ETCD_")})
    return values


def _etcd_members(settings: dict[str, str]) -> list[tuple[str, int]]:
    raw = (settings.get("ETCD_HOSTS") or settings.get("ETCD_HOST")
           or "localhost:2379")
    members = []
    for item in raw.split(","):
        item = item.strip().split("://", 1)[-1]
        if not item:
            continue
        host, _, port = item.rpartition(":")
        if host:
            members.append((host.strip("[]"), int(port)))
        else:
            members.append((item, 2379))
    return members


def inference_backends(model: str) -> list[str]:
    """Return the gateway's registered api_base URLs for `model`.

    Reads the same etcd key the proxy watches
    (`/cluster/config/inference/models/<model>`), so this is the routing
    table, not a guess. Returns [] for a model entry with no backends.
    Raises EtcdUnavailable when this host cannot read etcd (no etcd3
    module, no readable client cert, or no reachable member).
    """
    try:
        import etcd3
    except ImportError as e:
        raise EtcdUnavailable(f"python3-etcd3 is not installed ({e})")

    settings = _etcd_settings()
    tls = {}
    for env_key, kwarg in (("ETCD_CACERT", "ca_cert"),
                           ("ETCD_CERT", "cert_cert"),
                           ("ETCD_KEY", "cert_key")):
        if settings.get(env_key):
            tls[kwarg] = settings[env_key]

    errors = []
    value = None
    for host, port in _etcd_members(settings):
        client = None
        try:
            client = etcd3.client(host=host, port=port, timeout=5, **tls)
            value, _ = client.get(_ETCD_MODELS_PREFIX + model)
            break
        except Exception as e:  # noqa: BLE001 - try the next etcd member
            errors.append(f"{host}:{port}: {e}")
        finally:
            if client is not None:
                client.close()
    else:
        raise EtcdUnavailable("; ".join(errors) or "no etcd members configured")

    if not value:
        return []
    try:
        entry = json.loads(value)
    except ValueError as e:
        raise EtcdUnavailable(f"bad registry value for {model!r}: {e}")
    if not isinstance(entry, dict):
        raise EtcdUnavailable(f"unexpected registry value for {model!r}")
    backends = []
    for item in entry.get("backends") or []:
        url = item.get("api_base") if isinstance(item, dict) else item
        if url:
            backends.append(url.rstrip("/"))
    return backends
