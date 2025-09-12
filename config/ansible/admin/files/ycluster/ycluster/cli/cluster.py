"""
Cluster status and health management commands
"""

from ..utils import check_cluster

# Import the original check_cluster functionality
def register_cluster_commands(subparsers):
    """Register cluster management commands"""
    cluster_parser = subparsers.add_parser('cluster', help='Cluster status and health management')
    cluster_parser.set_defaults(func=lambda args: args.parser.print_help(), parser=cluster_parser)
    cluster_subparsers = cluster_parser.add_subparsers(dest='cluster_command', help='Cluster commands')
    
    # Status command
    status_parser = cluster_subparsers.add_parser('status', help='Check cluster health and status')
    status_parser.set_defaults(func=cluster_status)
    
    # Health command (alias for status)
    health_parser = cluster_subparsers.add_parser('health', help='Check cluster health (alias for status)')
    health_parser.set_defaults(func=cluster_status)
    
    # Populate local node command
    populate_parser = cluster_subparsers.add_parser('populate-local-node', help='Populate local node information in etcd')
    populate_parser.set_defaults(func=cluster_populate_local_node)


def cluster_status(args):
    """Execute cluster status check"""
    # Import and execute the original check_cluster main function
    check_cluster.main()


def cluster_populate_local_node(args):
    """Populate local node information in etcd"""
    from ..utils import populate_local_node
    populate_local_node.populate_local_node()
