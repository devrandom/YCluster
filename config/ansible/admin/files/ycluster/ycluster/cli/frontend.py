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


