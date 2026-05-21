"""Tiny helper: locate config.yml in the repo and return its values.

config.yml lives at the repo root and is gitignored. Schema:

    endpoint: https://your-cluster.example/
    api_token_file: host.token        # path relative to config.yml

Used by contrib/ scripts so the cluster endpoint isn't hardcoded.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ClusterConfig:
    endpoint: str          # e.g. "https://host.ycluster.net" (no trailing slash)
    token: str             # bearer token, read from api_token_file


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


def load(start: Path | None = None) -> ClusterConfig:
    path = _find_config((start or Path(__file__)).resolve())
    cfg = yaml.safe_load(path.read_text()) or {}
    endpoint = (cfg.get("endpoint") or "").rstrip("/")
    token_file = cfg.get("api_token_file")
    if not endpoint or not token_file:
        raise ValueError(f"{path}: `endpoint` and `api_token_file` are required")
    token = (path.parent / token_file).read_text().strip()
    return ClusterConfig(endpoint=endpoint, token=token)
