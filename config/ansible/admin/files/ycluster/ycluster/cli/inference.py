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
    # ycluster inference remove <url> [model]   (mirrors `add`; <url> alone = every model)
    remove_parser = sub.add_parser('remove', help='Remove a model, or a backend from one/all models')
    remove_parser.add_argument('target', nargs='?', default=None, help='Model name, or a backend URL (a value containing :// is treated as a backend)')
    remove_parser.add_argument('model', nargs='?', default=None, help='Model name, when the first argument is a backend URL')
    remove_parser.add_argument('--api-base', default=None, help='Backend to remove (alternative to giving the URL positionally)')
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

    # ycluster inference key ... — gateway API-key ownership (etcd system of
    # record, not Open-WebUI). See docs/design/api-key-ownership.md.
    key_parser = sub.add_parser('key', help='Manage inference gateway API keys (etcd-backed)')
    key_parser.set_defaults(func=lambda args: args.parser.print_help(), parser=key_parser)
    key_sub = key_parser.add_subparsers(dest='key_command', help='Key commands')

    key_create = key_sub.add_parser('create', help='Mint a new gateway API key (prints the secret once)')
    key_create.add_argument('--owner', required=True, help='Owner email (cluster identity key)')
    key_create.add_argument('--group', action='append', default=[], help='Group to grant (repeatable)')
    key_create.add_argument('--label', default=None, help='Human-readable label')
    key_create.set_defaults(func=_cmd_key_create)

    key_list = key_sub.add_parser('list', help='List key metadata (never the secret)')
    key_list.add_argument('--owner', default=None, help='Only this owner (default: all)')
    key_list.set_defaults(func=_cmd_key_list)

    key_revoke = key_sub.add_parser('revoke', help='Revoke a key by its key-id (idempotent)')
    key_revoke.add_argument('key_id', help='Key id (from `ycluster inference key list`)')
    key_revoke.set_defaults(func=_cmd_key_revoke)

    key_migrate = key_sub.add_parser('migrate', help='One-time: copy Open-WebUI keys into etcd (same token keeps working, no rotation)')
    key_migrate.set_defaults(func=_cmd_key_migrate)


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
    # local-ai-proxy uses Go's flag package, which stops parsing at the first
    # positional — so flags must precede positionals. The proxy CLI sniffs a
    # '://' positional as a backend URL, so we just forward verbatim.
    positionals = [a for a in (args.target, args.model) if a]
    if not positionals and not args.api_base:
        print("error: specify a model, or a backend URL / --api-base", file=sys.stderr)
        sys.exit(2)
    argv = ['models', 'remove']
    if args.api_base:
        argv += ['--api-base', args.api_base]
    argv += positionals
    _forward(*argv)


def _cmd_disable(args):
    # Flags must precede the positional — see _cmd_remove.
    argv = ['backends', 'disable']
    if args.reason:
        argv += ['--reason', args.reason]
    argv.append(args.url)
    _forward(*argv)


def _cmd_status(args):
    if args.proxy_url:
        inference_manager.show_status(args.proxy_url)
    else:
        inference_manager.show_status()


def _cmd_key_create(args):
    from ..common import apikeys
    try:
        secret = apikeys.create_key(args.owner, groups=args.group, label=args.label)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"error: etcd write failed: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Created key for {args.owner}. Copy it now — it is shown only once:")
    print(f"  {secret}")
    if args.group:
        print(f"Groups: {', '.join(args.group)}")


def _cmd_key_list(args):
    from ..common import apikeys
    try:
        data = apikeys.list_keys(args.owner)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"error: etcd read failed: {e}", file=sys.stderr)
        sys.exit(1)
    fmt = "{:<36} {:<40} {:<14} {}"
    print(fmt.format('KEY ID', 'OWNER', 'CREATED', 'LABEL'))
    for email in sorted(data):
        for rec in data[email]:
            print(fmt.format(rec['key_id'], email,
                             rec['created_at'][:13].replace('T', ' '),
                             rec['label']))
        if not data[email]:
            print(fmt.format('', email, '', '(no keys)'))


def _cmd_key_revoke(args):
    from ..common import apikeys
    try:
        removed = apikeys.revoke_key(args.key_id)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"error: etcd write failed: {e}", file=sys.stderr)
        sys.exit(1)
    if removed:
        print(f"Revoked key {args.key_id}.")
    else:
        print(f"No key with id {args.key_id} (no-op).")


def _cmd_key_migrate(args):
    from ..common import apikeys
    try:
        count = apikeys.migrate_owui_keys()
    except Exception as e:
        print(f"error: migration failed: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Migrated {count} key(s) from Open-WebUI into etcd (same tokens keep working).")
