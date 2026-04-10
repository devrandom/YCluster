"""
WireGuard overlay management commands.
"""

import json
import sys

from ..utils import wg_config


def register_wg_commands(subparsers):
    p = subparsers.add_parser('wg', help='WireGuard overlay management')
    p.set_defaults(func=lambda args: args.parser.print_help(), parser=p)
    sub = p.add_subparsers(dest='wg_command', help='wg commands')

    init_p = sub.add_parser('init', help='Initialize wg server (generate keys, set endpoints)')
    init_p.add_argument('endpoints', nargs='+', metavar='HOST[:PORT]',
                        help='Public endpoint(s) clients will dial. Port defaults to 51820.')
    init_p.add_argument('--port', type=int, help='Listen port (default: 51820)')
    init_p.add_argument('--rotate', action='store_true', help='Rotate server keypair')
    init_p.set_defaults(func=_init)

    show_p = sub.add_parser('show', help='Show server configuration')
    show_p.set_defaults(func=_show)

    list_p = sub.add_parser('list', help='List peers')
    list_p.add_argument('--pending', action='store_true', help='Only show pending')
    list_p.add_argument('--approved', action='store_true', help='Only show approved')
    list_p.add_argument('--json', dest='as_json', action='store_true')
    list_p.set_defaults(func=_list)

    ap = sub.add_parser('approve', help='Approve a pending peer')
    ap.add_argument('hostname')
    ap.set_defaults(func=_approve)

    rv = sub.add_parser('revoke', help='Revoke a peer')
    rv.add_argument('hostname')
    rv.set_defaults(func=_revoke)

    rm = sub.add_parser('delete', help='Delete a peer record entirely')
    rm.add_argument('hostname')
    rm.set_defaults(func=_delete)

    rc = sub.add_parser('reconcile', help='Apply etcd peer state to local wg0')
    rc.add_argument('--up', action='store_true', help='Bring wg0 up if not running')
    rc.add_argument('--down', action='store_true', help='Bring wg0 down')
    rc.set_defaults(func=_reconcile)

    rend = sub.add_parser('render', help='Print rendered config to stdout')
    rend.add_argument('--client', metavar='HOSTNAME', help='Render client config for hostname')
    rend.set_defaults(func=_render)


def _init(args):
    server = wg_config.init_server(args.endpoints, port=args.port, rotate=args.rotate)
    print(f"wg server initialized")
    print(f"  pubkey:    {server['pubkey']}")
    print(f"  port:      {server['port']}")
    print(f"  endpoints: {', '.join(server['endpoints'])}")
    print(f"  server_ip: {server['server_ip']}")


def _show(args):
    server = wg_config.get_server()
    if not server:
        print("not initialized", file=sys.stderr)
        sys.exit(1)
    redacted = {k: v for k, v in server.items() if k != 'privkey'}
    redacted['privkey'] = '***'
    print(json.dumps(redacted, indent=2))


def _list(args):
    peers = wg_config.list_peers()
    if args.pending:
        peers = [p for p in peers if p[1]['status'] == 'pending']
    if args.approved:
        peers = [p for p in peers if p[1]['status'] == 'approved']

    if args.as_json:
        out = []
        for hostname, peer, node in peers:
            out.append({
                'hostname': hostname,
                'status': peer['status'],
                'ip': node['ip'] if node else None,
                'type': node['type'] if node else None,
                'pubkey_sha256': peer.get('pubkey_sha256'),
                'created_at': peer.get('created_at'),
                'approved_at': peer.get('approved_at'),
            })
        print(json.dumps(out, indent=2))
        return

    if not peers:
        print("no peers")
        return
    print(f"{'HOSTNAME':<10} {'STATUS':<10} {'TYPE':<10} {'IP':<16} {'FP':<18} CREATED")
    for hostname, peer, node in peers:
        ip = node['ip'] if node else '-'
        ntype = node['type'] if node else '-'
        fp = peer.get('pubkey_sha256', '-')
        print(f"{hostname:<10} {peer['status']:<10} {ntype:<10} {ip:<16} {fp:<18} {peer.get('created_at','')}")


def _approve(args):
    peer = wg_config.set_peer_status(args.hostname, 'approved')
    print(f"{args.hostname}: approved")
    try:
        wg_config.reconcile()
    except Exception as e:
        print(f"warning: reconcile failed: {e}", file=sys.stderr)


def _revoke(args):
    wg_config.set_peer_status(args.hostname, 'revoked')
    print(f"{args.hostname}: revoked")
    try:
        wg_config.reconcile()
    except Exception as e:
        print(f"warning: reconcile failed: {e}", file=sys.stderr)


def _delete(args):
    wg_config.delete_peer(args.hostname)
    print(f"{args.hostname}: deleted")
    try:
        wg_config.reconcile()
    except Exception as e:
        print(f"warning: reconcile failed: {e}", file=sys.stderr)


def _reconcile(args):
    wg_config.reconcile(up=args.up, down=args.down)


def _render(args):
    if args.client:
        print(wg_config.render_client_config(args.client))
    else:
        print(wg_config.render_server_config())
