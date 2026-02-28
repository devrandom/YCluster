"""
Inference gateway management commands (LiteLLM)
"""

from ..utils import inference_manager


def register_inference_commands(subparsers):
    """Register inference management commands"""
    parser = subparsers.add_parser('inference', help='Inference gateway management (LiteLLM)')
    parser.set_defaults(func=lambda args: args.parser.print_help(), parser=parser)
    sub = parser.add_subparsers(dest='inference_command', help='Inference commands')

    # ycluster inference models
    models_parser = sub.add_parser('models', help='List configured models (from config file)')
    models_parser.set_defaults(func=inference_models)

    # ycluster inference ls
    ls_parser = sub.add_parser('ls', help='List live models from the LiteLLM backend')
    ls_parser.set_defaults(func=inference_ls)

    # ycluster inference add <model-name> <api-base> [--backend-model <name>]
    add_parser = sub.add_parser('add', help='Add a model backend')
    add_parser.add_argument('model_name', help='User-facing model name (e.g. qwen3-32b)')
    add_parser.add_argument('api_base', help='Backend URL — shorthand allowed (e.g. nv1.xc -> http://nv1.xc:8000/v1)')
    add_parser.add_argument('--backend-model', help='Backend model identifier (default: openai/<model-name>)')
    add_parser.set_defaults(func=inference_add)

    # ycluster inference remove <model-name> [--api-base <url>]
    remove_parser = sub.add_parser('remove', help='Remove a model (or specific backend)')
    remove_parser.add_argument('model_name', help='Model name to remove')
    remove_parser.add_argument('--api-base', help='Remove only this specific backend (default: remove all)')
    remove_parser.set_defaults(func=inference_remove)

    # ycluster inference reload
    reload_parser = sub.add_parser('reload', help='Reload LiteLLM configuration')
    reload_parser.set_defaults(func=inference_reload)


def inference_models(args):
    """List configured models from config file"""
    inference_manager.list_models()


def inference_ls(args):
    """List live models from the LiteLLM backend"""
    inference_manager.list_live_models()


def inference_add(args):
    """Add a model backend"""
    inference_manager.add_model(args.model_name, args.api_base, args.backend_model)


def inference_remove(args):
    """Remove a model"""
    inference_manager.remove_model(args.model_name, args.api_base)


def inference_reload(args):
    """Reload LiteLLM configuration"""
    inference_manager.reload_litellm()
