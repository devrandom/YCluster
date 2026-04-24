"""
Inference gateway management commands.

Thin wrapper around the `local-ai-proxy` binary's own `models` and
`backends` subcommands — the proxy owns its etcd schema, so we just
forward arguments and exit code. `status` keeps a custom renderer
because the /healthz payload is proxy-specific and nicer to format
in Python.
"""

import subprocess
import sys

from ..utils import inference_manager

LOCAL_AI_PROXY_BIN = "/usr/local/bin/local-ai-proxy"


def register_inference_commands(subparsers):
    """Register inference management commands"""
    parser = subparsers.add_parser('inference', help='Inference gateway management (local-ai-proxy)')
    parser.set_defaults(func=lambda args: args.parser.print_help(), parser=parser)
    sub = parser.add_subparsers(dest='inference_command', help='Inference commands')

    # ycluster inference ls / models — forward to `local-ai-proxy models ls`
    for name, help_text in [
        ('ls', 'List configured models (alias for models)'),
        ('models', 'List configured models with backends'),
    ]:
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(func=lambda args: _forward('models', 'ls'))

    # ycluster inference add <api-base> [model]
    add_parser = sub.add_parser('add', help='Add model(s) from a backend (auto-discover if model omitted)')
    add_parser.add_argument('api_base', help='Backend URL, e.g. http://nv1.xc:8000')
    add_parser.add_argument('model', nargs='?', default=None, help='Model name (omit to auto-discover)')
    add_parser.set_defaults(func=_cmd_add)

    # ycluster inference remove <model> [--api-base <url>]
    remove_parser = sub.add_parser('remove', help='Remove a model (or a specific backend from it)')
    remove_parser.add_argument('model', help='Model name')
    remove_parser.add_argument('--api-base', default=None, help='Remove only this backend (default: remove whole model)')
    remove_parser.set_defaults(func=_cmd_remove)

    # ycluster inference disable <url> [--reason ...]
    disable_parser = sub.add_parser('disable', help='Mark a backend URL as known-down')
    disable_parser.add_argument('url', help='Backend URL')
    disable_parser.add_argument('--reason', default=None, help='Human-readable reason')
    disable_parser.set_defaults(func=_cmd_disable)

    # ycluster inference enable <url>
    enable_parser = sub.add_parser('enable', help='Remove a backend URL from the disabled set')
    enable_parser.add_argument('url', help='Backend URL')
    enable_parser.set_defaults(func=lambda args: _forward('backends', 'enable', args.url))

    # ycluster inference status
    status_parser = sub.add_parser('status', help='Show local-ai-proxy backend + model health')
    status_parser.add_argument('--proxy-url', default=None, help='Override proxy URL (default: http://localhost:4001)')
    status_parser.set_defaults(func=_cmd_status)

    # ycluster inference reload
    reload_parser = sub.add_parser('reload', help='Restart local-ai-proxy (picks up YAML config changes; model edits are hot)')
    reload_parser.set_defaults(func=lambda args: inference_manager.reload_proxy())


def _forward(*args):
    """Exec local-ai-proxy with the given arguments and exit with its code."""
    try:
        proc = subprocess.run([LOCAL_AI_PROXY_BIN, *args])
    except FileNotFoundError:
        print(f"{LOCAL_AI_PROXY_BIN} not found. Is local-ai-proxy installed on this host?", file=sys.stderr)
        sys.exit(1)
    sys.exit(proc.returncode)


def _cmd_add(args):
    argv = ['models', 'add', args.api_base]
    if args.model:
        argv.append(args.model)
    _forward(*argv)


def _cmd_remove(args):
    argv = ['models', 'remove', args.model]
    if args.api_base:
        argv += ['--api-base', args.api_base]
    _forward(*argv)


def _cmd_disable(args):
    argv = ['backends', 'disable', args.url]
    if args.reason:
        argv += ['--reason', args.reason]
    _forward(*argv)


def _cmd_status(args):
    if args.proxy_url:
        inference_manager.show_status(args.proxy_url)
    else:
        inference_manager.show_status()
