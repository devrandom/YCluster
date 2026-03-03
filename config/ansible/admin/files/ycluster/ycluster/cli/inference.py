"""
Inference gateway management commands (LiteLLM)
"""

import sys

from ..utils import inference_manager


def register_inference_commands(subparsers):
    """Register inference management commands"""
    parser = subparsers.add_parser('inference', help='Inference gateway management (LiteLLM)')
    parser.set_defaults(func=lambda args: args.parser.print_help(), parser=parser)
    sub = parser.add_subparsers(dest='inference_command', help='Inference commands')

    # ycluster inference models
    models_parser = sub.add_parser('models', help='List configured models with backends')
    models_parser.set_defaults(func=inference_models)

    # ycluster inference ls (alias for models)
    ls_parser = sub.add_parser('ls', help='List configured models with backends (alias for models)')
    ls_parser.set_defaults(func=inference_models)

    # ycluster inference add <api-base> [model-name] [--backend-model <name>] [key=value ...]
    add_parser = sub.add_parser('add', help='Add model(s) from a backend')
    add_parser.add_argument('api_base', help='Backend URL — shorthand allowed (e.g. nv1.xc -> http://nv1.xc:8000/v1)')
    add_parser.add_argument('model_name', nargs='?', default=None, help='Model name (omit to auto-discover all models from backend)')
    add_parser.add_argument('--backend-model', help='Backend model identifier (default: openai/<model-name>)')
    add_parser.add_argument('extra_params', nargs='*', default=[], help='Extra litellm_params as key=value (e.g. max_parallel_requests=64)')
    add_parser.set_defaults(func=inference_add)

    # ycluster inference remove <model-name> [--api-base <url>]
    remove_parser = sub.add_parser('remove', help='Remove a model (or specific backend)')
    remove_parser.add_argument('model_name', help='Model name to remove')
    remove_parser.add_argument('--api-base', help='Remove only this specific backend (default: remove all)')
    remove_parser.set_defaults(func=inference_remove)

    # ycluster inference key
    key_parser = sub.add_parser('key', help='Print the LiteLLM master API key')
    key_parser.set_defaults(func=inference_key)

    # ycluster inference reload
    reload_parser = sub.add_parser('reload', help='Restart LiteLLM (for config.yaml changes only; model add/remove is instant)')
    reload_parser.set_defaults(func=inference_reload)


def inference_models(args):
    """List configured models"""
    inference_manager.list_models()



def _parse_extra_params(extra_params):
    """Parse key=value pairs into a dict, auto-converting numeric values."""
    params = {}
    for item in extra_params:
        if '=' not in item:
            print(f"Invalid parameter (expected key=value): {item}")
            sys.exit(1)
        key, value = item.split('=', 1)
        # Auto-convert numeric values
        try:
            value = int(value)
        except ValueError:
            try:
                value = float(value)
            except ValueError:
                pass
        params[key] = value
    return params


def inference_add(args):
    """Add a model backend"""
    extra = _parse_extra_params(args.extra_params)
    inference_manager.add_model(args.model_name, args.api_base, args.backend_model, extra)


def inference_remove(args):
    """Remove a model"""
    inference_manager.remove_model(args.model_name, args.api_base)


def inference_key(args):
    """Print the LiteLLM master API key"""
    inference_manager.print_master_key()


def inference_reload(args):
    """Reload LiteLLM configuration"""
    inference_manager.reload_litellm()
