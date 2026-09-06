"""
Frontend node management commands
"""

from ..utils import frontend_manager

def register_frontend_commands(subparsers):
    """Register frontend management commands"""
    frontend_parser = subparsers.add_parser('frontend', help='Frontend node management')
    frontend_parser.set_defaults(func=lambda args: args.parser.print_help(), parser=frontend_parser)
    frontend_subparsers = frontend_parser.add_subparsers(dest='frontend_command', help='Frontend commands')
    
    # List command
    list_parser = frontend_subparsers.add_parser('list', help='List all frontend nodes')
    list_parser.set_defaults(func=frontend_list)
    
    # Add command
    add_parser = frontend_subparsers.add_parser('add', help='Add a frontend node')
    add_parser.add_argument('name', help='Node name')
    add_parser.add_argument('address', help='IP address or hostname')
    add_parser.add_argument('--description', help='Optional description')
    add_parser.set_defaults(func=frontend_add)
    
    # Delete command
    del_parser = frontend_subparsers.add_parser('delete', help='Delete a frontend node')
    del_parser.add_argument('name', help='Node name to delete')
    del_parser.set_defaults(func=frontend_delete)
    
    # Show command
    show_parser = frontend_subparsers.add_parser('show', help='Show frontend node details')
    show_parser.add_argument('name', help='Node name to show')
    show_parser.set_defaults(func=frontend_show)

    # NAT exposure commands (vm-frontend-nat design)
    expose_parser = frontend_subparsers.add_parser('expose', help='Expose a VM port range on the frontend NAT gateway')
    expose_parser.add_argument('vm', help='VM name')
    expose_parser.add_argument('--proto', choices=['tcp', 'udp'], required=True)
    expose_parser.add_argument('--ports', required=True, help='Port or range, e.g. 20000-20099')
    expose_parser.add_argument('--transit-ip', help='Transit network IP (100.100.<host>.x); set once allocated')
    expose_parser.set_defaults(func=frontend_expose)

    unexpose_parser = frontend_subparsers.add_parser('unexpose', help='Remove a VM port exposure')
    unexpose_parser.add_argument('vm', help='VM name')
    unexpose_parser.add_argument('--proto', choices=['tcp', 'udp'], required=True)
    unexpose_parser.add_argument('--ports', required=True)
    unexpose_parser.set_defaults(func=frontend_unexpose)

    exposures_parser = frontend_subparsers.add_parser('exposures', help='List all NAT port exposures')
    exposures_parser.set_defaults(func=frontend_exposures)

    render_parser = frontend_subparsers.add_parser('render-nft', help='Render the frontend nftables ruleset to stdout')
    render_parser.add_argument('--public-ip', required=True)
    render_parser.add_argument('--wan-iface', default='eth0')
    render_parser.add_argument('--transit-iface', default='wg-vm-nat')
    render_parser.set_defaults(func=frontend_render_nft)


def frontend_expose(args):
    """Store one NAT exposure mapping"""
    from ..utils import frontend_nat
    key = frontend_nat.set_exposure(args.vm, args.proto, args.ports,
                                    args.transit_ip)
    print(f'Set {key}')


def frontend_unexpose(args):
    """Remove one NAT exposure mapping"""
    from ..utils import frontend_nat
    frontend_nat.delete_exposure(args.vm, args.proto, args.ports)
    print(f'Removed {args.proto}/{args.vm}/{args.ports}')


def frontend_exposures(args):
    """List all NAT exposure mappings"""
    from ..utils import frontend_nat
    exposures = frontend_nat.list_exposures()
    if not exposures:
        print('No NAT exposures configured.')
        return
    for e in exposures:
        print(f'{e["proto"]:3} {e["ports"]:15} {e["vm"]:20} {e["transit_ip"] or "-"}')


def frontend_render_nft(args):
    """Render the frontend nftables ruleset"""
    from ..utils import frontend_nat
    print(frontend_nat.render_nftables(
        frontend_nat.list_exposures(), args.public_ip,
        args.wan_iface, args.transit_iface), end='')


def frontend_list(args):
    """List frontend nodes"""
    frontend_manager.list_frontend_nodes()


def frontend_add(args):
    """Add frontend node"""
    frontend_manager.add_frontend_node(args.name, args.address, args.description)


def frontend_delete(args):
    """Delete frontend node"""
    frontend_manager.delete_frontend_node(args.name)


def frontend_show(args):
    """Show frontend node"""
    frontend_manager.show_frontend_node(args.name)


