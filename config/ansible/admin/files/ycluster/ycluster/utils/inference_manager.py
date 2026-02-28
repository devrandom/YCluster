"""
Inference gateway management — model list and LiteLLM lifecycle.

The model list lives at /rbd/misc/litellm/models.yaml (Ceph-backed).
LiteLLM reads it on startup via the `include` directive in config.yaml.
Reloading restarts the litellm container so it re-reads the file.
"""

import subprocess
import sys
from urllib.parse import urlparse

import requests
import yaml

MODELS_FILE = "/rbd/misc/litellm/models.yaml"
LITELLM_URL = "http://localhost:4000"


def _get_master_key():
    """Read the LiteLLM master key from etcd."""
    from ..common.etcd_utils import get_etcd_client

    client = get_etcd_client()
    value, _ = client.get("/cluster/config/litellm/master-key")
    if not value:
        print("LiteLLM master key not found in etcd.")
        print("Has LiteLLM been started at least once?")
        sys.exit(1)
    return value.decode()


def normalize_api_base(url):
    """Normalize a URL shorthand to a full API base URL.

    Examples:
        nv1.xc          -> http://nv1.xc:8000/v1
        nv1.xc:8080     -> http://nv1.xc:8080/v1
        nv1.xc:8080/v1  -> http://nv1.xc:8080/v1
        http://nv1.xc:8000/v1 -> http://nv1.xc:8000/v1  (unchanged)
    """
    # If no scheme, add http://
    if "://" not in url:
        url = "http://" + url

    parsed = urlparse(url)

    # Default port to 8000 if not specified
    host = parsed.hostname or ""
    port = parsed.port or 8000

    # Default path to /v1 if empty or just /
    path = parsed.path
    if not path or path == "/":
        path = "/v1"

    return f"{parsed.scheme}://{host}:{port}{path}"


def _read_models():
    """Read and parse the models.yaml file."""
    try:
        with open(MODELS_FILE, "r") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        print(f"Models file not found: {MODELS_FILE}")
        print("Is the Ceph volume mounted? Is LiteLLM deployed?")
        sys.exit(1)

    if not data or "model_list" not in data:
        return []
    return data["model_list"]


def _write_models(model_list):
    """Write the model list back to models.yaml, preserving the header comment."""
    header = (
        "# LiteLLM Model List\n"
        "#\n"
        "# This file lives on Ceph at /rbd/misc/litellm/models.yaml and can be edited\n"
        "# directly on any core node. After editing, reload with:\n"
        "#   ycluster inference reload\n"
        "\n"
    )
    with open(MODELS_FILE, "w") as f:
        f.write(header)
        yaml.dump(
            {"model_list": model_list},
            f,
            default_flow_style=False,
            sort_keys=False,
        )


def list_models():
    """Print all configured models (from the YAML config file)."""
    models = _read_models()
    if not models:
        print("No models configured.")
        return

    # Group by model_name for a cleaner display
    seen = {}
    for entry in models:
        name = entry.get("model_name", "?")
        params = entry.get("litellm_params", {})
        api_base = params.get("api_base", "?")
        model = params.get("model", "?")
        if name not in seen:
            seen[name] = []
        seen[name].append({"model": model, "api_base": api_base})

    for name, backends in seen.items():
        if len(backends) == 1:
            b = backends[0]
            print(f"  {name}  ->  {b['api_base']}  ({b['model']})")
        else:
            print(f"  {name}  ({len(backends)} backends)")
            for b in backends:
                print(f"    - {b['api_base']}  ({b['model']})")


def list_live_models():
    """Query LiteLLM API and print currently active models."""
    master_key = _get_master_key()

    try:
        resp = requests.get(
            f"{LITELLM_URL}/v1/models",
            headers={"Authorization": f"Bearer {master_key}"},
            timeout=10,
        )
    except requests.ConnectionError:
        print("Cannot connect to LiteLLM at", LITELLM_URL)
        print("Is the litellm service running?")
        sys.exit(1)

    if resp.status_code != 200:
        print(f"LiteLLM returned HTTP {resp.status_code}: {resp.text}")
        sys.exit(1)

    data = resp.json()
    models = data.get("data", [])

    if not models:
        print("No models currently active in LiteLLM.")
        return

    for m in models:
        model_id = m.get("id", "?")
        print(f"  {model_id}")


def add_model(model_name, api_base, backend_model=None):
    """Add a model entry to the config."""
    models = _read_models()
    api_base = normalize_api_base(api_base)

    if backend_model is None:
        backend_model = f"openai/{model_name}"

    entry = {
        "model_name": model_name,
        "litellm_params": {
            "model": backend_model,
            "api_base": api_base,
            "api_key": "none",
        },
    }

    models.append(entry)
    _write_models(models)
    print(f"Added: {model_name} -> {api_base} ({backend_model})")
    print("Run 'ycluster inference reload' to apply changes.")


def remove_model(model_name, api_base=None):
    """Remove model entries from the config.

    If api_base is given, only remove that specific backend.
    Otherwise, remove all entries for the model_name.
    """
    models = _read_models()
    original_count = len(models)

    if api_base:
        api_base = normalize_api_base(api_base)
        models = [
            m
            for m in models
            if not (
                m.get("model_name") == model_name
                and m.get("litellm_params", {}).get("api_base") == api_base
            )
        ]
    else:
        models = [m for m in models if m.get("model_name") != model_name]

    removed = original_count - len(models)
    if removed == 0:
        print(f"No matching entries found for '{model_name}'")
        if api_base:
            print(f"  (with api_base={api_base})")
        sys.exit(1)

    _write_models(models)
    print(f"Removed {removed} entry/entries for '{model_name}'")
    print("Run 'ycluster inference reload' to apply changes.")


def reload_litellm():
    """Restart the LiteLLM container to reload configuration."""
    print("Restarting LiteLLM container...")
    result = subprocess.run(
        ["docker", "restart", "litellm"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Failed to restart LiteLLM: {result.stderr.strip()}")
        sys.exit(1)
    print("LiteLLM restarted. Configuration reloaded.")
