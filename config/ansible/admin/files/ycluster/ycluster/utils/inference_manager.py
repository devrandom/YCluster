"""
Inference gateway management — model list and LiteLLM lifecycle.

Models are stored in LiteLLM's PostgreSQL database (STORE_MODEL_IN_DB=True).
The CLI manages models via LiteLLM's /model/new and /model/delete APIs,
which take effect immediately without restarting the service.

A seed models.yaml is loaded on first boot via the config.yaml `include`
directive, but all subsequent changes go through the API.
"""

import subprocess
import sys
from urllib.parse import urlparse

import requests

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


def _litellm_headers():
    """Return auth headers for LiteLLM API calls."""
    return {"Authorization": f"Bearer {_get_master_key()}"}


def _litellm_request(method, path, **kwargs):
    """Make a request to the LiteLLM API, handling connection errors."""
    kwargs.setdefault("timeout", 10)
    try:
        resp = getattr(requests, method)(
            f"{LITELLM_URL}{path}",
            headers=_litellm_headers(),
            **kwargs,
        )
    except requests.ConnectionError:
        print(f"Cannot connect to LiteLLM at {LITELLM_URL}")
        print("Is the litellm service running?")
        sys.exit(1)
    return resp


def normalize_api_base(url):
    """Normalize a URL shorthand to a full API base URL.

    Examples:
        nv1.xc          -> http://nv1.xc:8000/v1
        nv1.xc:8080     -> http://nv1.xc:8080/v1
        nv1.xc:8080/v1  -> http://nv1.xc:8080/v1
        http://nv1.xc:8000/v1 -> http://nv1.xc:8000/v1  (unchanged)
    """
    if "://" not in url:
        url = "http://" + url

    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or 8000
    path = parsed.path
    if not path or path == "/":
        path = "/v1"

    return f"{parsed.scheme}://{host}:{port}{path}"


def _get_model_info():
    """Fetch all model info from LiteLLM (includes db_model flag and IDs)."""
    resp = _litellm_request("get", "/v1/model/info")
    if resp.status_code != 200:
        print(f"LiteLLM returned HTTP {resp.status_code}: {resp.text}")
        sys.exit(1)
    return resp.json().get("data", [])


def list_models():
    """Print all configured models from LiteLLM."""
    models = _get_model_info()
    if not models:
        print("No models configured.")
        return

    # Group by model_name for cleaner display
    seen = {}
    for entry in models:
        name = entry.get("model_name", "?")
        params = entry.get("litellm_params", {})
        api_base = params.get("api_base", "?")
        model = params.get("model", "?")
        db = entry.get("model_info", {}).get("db_model", False)
        if name not in seen:
            seen[name] = []
        seen[name].append({"model": model, "api_base": api_base, "db": db})

    for name, backends in seen.items():
        if len(backends) == 1:
            b = backends[0]
            src = "db" if b["db"] else "config"
            print(f"  {name}  ->  {b['api_base']}  ({src})")
        else:
            print(f"  {name}  ({len(backends)} backends)")
            for b in backends:
                src = "db" if b["db"] else "config"
                print(f"    - {b['api_base']}  ({src})")


def list_live_models():
    """Query LiteLLM /v1/models and print active model names."""
    resp = _litellm_request("get", "/v1/models")
    if resp.status_code != 200:
        print(f"LiteLLM returned HTTP {resp.status_code}: {resp.text}")
        sys.exit(1)

    models = resp.json().get("data", [])
    if not models:
        print("No models currently active in LiteLLM.")
        return

    for m in models:
        print(f"  {m.get('id', '?')}")


def _discover_backend_models(api_base):
    """Query a backend's /v1/models endpoint and return a list of model IDs."""
    try:
        resp = requests.get(f"{api_base}/models", timeout=10)
    except requests.ConnectionError:
        print(f"Cannot connect to backend at {api_base}")
        sys.exit(1)

    if resp.status_code != 200:
        print(f"Backend returned HTTP {resp.status_code}: {resp.text}")
        sys.exit(1)

    return [m["id"] for m in resp.json().get("data", [])]


def _get_existing_pairs():
    """Return set of (model_name, api_base) for all currently configured models."""
    models = _get_model_info()
    return {
        (m.get("model_name"), m.get("litellm_params", {}).get("api_base"))
        for m in models
    }


def _api_add_model(model_name, api_base, backend_model):
    """Add a single model via the LiteLLM /model/new API."""
    resp = _litellm_request(
        "post",
        "/model/new",
        json={
            "model_name": model_name,
            "litellm_params": {
                "model": backend_model,
                "api_base": api_base,
                "api_key": "none",
            },
        },
    )
    if resp.status_code != 200:
        error = resp.json().get("error", {})
        msg = error.get("message", resp.text) if isinstance(error, dict) else error
        print(f"  error: {model_name} — {msg}")
        return False
    return True


def add_model(model_name, api_base, backend_model=None):
    """Add model(s) via the LiteLLM API (immediate, no restart needed).

    If model_name is None, auto-discover all models served by the backend.
    Otherwise add a single named model.
    """
    api_base = normalize_api_base(api_base)

    if model_name is None:
        backend_models = _discover_backend_models(api_base)
        if not backend_models:
            print(f"No models found at {api_base}")
            sys.exit(1)
        _add_multiple(backend_models, api_base)
    else:
        if backend_model is None:
            backend_model = f"openai/{model_name}"
        if _api_add_model(model_name, api_base, backend_model):
            print(f"Added: {model_name} -> {api_base} ({backend_model})")


def _add_multiple(backend_models, api_base):
    """Add multiple auto-discovered models, skipping duplicates."""
    existing = _get_existing_pairs()

    added = 0
    skipped = 0
    for model_id in backend_models:
        if (model_id, api_base) in existing:
            print(f"  skip: {model_id} (already configured for {api_base})")
            skipped += 1
            continue

        if _api_add_model(model_id, api_base, f"openai/{model_id}"):
            print(f"  added: {model_id} -> {api_base}")
            added += 1

    if added > 0:
        print(f"\nAdded {added} model(s).", end="")
        if skipped:
            print(f" Skipped {skipped} (already configured).", end="")
        print()
    else:
        print(f"All {skipped} model(s) already configured for {api_base}.")


def remove_model(model_name, api_base=None):
    """Remove model entries via the LiteLLM /model/delete API.

    If api_base is given, only remove that specific backend.
    Otherwise, remove all entries for the model_name.
    """
    if api_base:
        api_base = normalize_api_base(api_base)

    models = _get_model_info()
    to_delete = []

    for entry in models:
        if entry.get("model_name") != model_name:
            continue
        entry_api_base = entry.get("litellm_params", {}).get("api_base")
        if api_base and entry_api_base != api_base:
            continue
        model_id = entry.get("model_info", {}).get("id")
        db_model = entry.get("model_info", {}).get("db_model", False)
        if model_id:
            to_delete.append((model_id, entry_api_base, db_model))

    if not to_delete:
        print(f"No matching entries found for '{model_name}'")
        if api_base:
            print(f"  (with api_base={api_base})")
        sys.exit(1)

    removed = 0
    for model_id, entry_base, db_model in to_delete:
        if not db_model:
            print(f"  skip: {entry_base} (from config file, not DB — remove from config.yaml and restart)")
            continue
        resp = _litellm_request(
            "post",
            "/model/delete",
            json={"id": model_id},
        )
        if resp.status_code == 200:
            print(f"  removed: {model_name} ({entry_base})")
            removed += 1
        else:
            error = resp.json().get("error", resp.text)
            print(f"  error deleting {model_id}: {error}")

    if removed > 0:
        print(f"Removed {removed} entry/entries for '{model_name}'")
    elif all(not d[2] for d in to_delete):
        print("All matching entries are from the config file.")
        print("Edit the config and run 'ycluster inference reload' to remove them.")


def reload_litellm():
    """Restart the LiteLLM service to reload static configuration.

    Note: model add/remove no longer requires a reload — they take effect
    immediately via the API. Use reload only for changes to config.yaml
    (e.g. router settings, auth config).
    """
    print("Restarting LiteLLM service...")
    result = subprocess.run(
        ["systemctl", "restart", "litellm"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Failed to restart LiteLLM: {result.stderr.strip()}")
        sys.exit(1)
    print("LiteLLM restarted. Static configuration reloaded.")
    print("Note: model add/remove takes effect immediately and does not need reload.")
