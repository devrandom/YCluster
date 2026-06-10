"""
Cluster status and health management commands
"""

from ..utils import check_cluster


def register_cluster_commands(subparsers):
    """Register cluster management commands"""
    cluster_parser = subparsers.add_parser('cluster', help='Cluster status and health management')
    cluster_parser.set_defaults(func=lambda args: args.parser.print_help(), parser=cluster_parser)
    cluster_subparsers = cluster_parser.add_subparsers(dest='cluster_command', help='Cluster commands')

    # Status command (verbose by default for backward compat)
    status_parser = cluster_subparsers.add_parser('status', help='Detailed cluster status (verbose)')
    status_parser.set_defaults(func=cluster_status_verbose)

    # Health command (compact by default)
    health_parser = cluster_subparsers.add_parser('health', help='Compact cluster health summary')
    health_parser.add_argument('-v', '--verbose', action='store_true', help='Show full detailed output')
    health_parser.set_defaults(func=cluster_health)

    # Top-level health alias is registered in register_health_alias()
    

    # Populate local node command
    populate_parser = cluster_subparsers.add_parser('populate-local-node', help='Populate local node information in etcd')
    populate_parser.set_defaults(func=cluster_populate_local_node)

    # Host disable command
    disable_parser = cluster_subparsers.add_parser('disable', help='Disable a host (exclude from status page)')
    disable_parser.add_argument('hostname', help='Hostname to disable (e.g., c1)')
    disable_parser.set_defaults(func=cluster_disable_host)

    # Host enable command
    enable_parser = cluster_subparsers.add_parser('enable', help='Re-enable a host (include in status page)')
    enable_parser.add_argument('hostname', help='Hostname to enable (e.g., c1)')
    enable_parser.set_defaults(func=cluster_enable_host)

    # Drain command
    drain_parser = cluster_subparsers.add_parser('drain', help='Drain a node (disable leader election)')
    drain_parser.add_argument('hostname', help='Hostname to drain (e.g., s2)')
    drain_parser.set_defaults(func=cluster_drain_host)

    # Undrain command
    undrain_parser = cluster_subparsers.add_parser('undrain', help='Undrain a node (re-enable leader election)')
    undrain_parser.add_argument('hostname', help='Hostname to undrain (e.g., s2)')
    undrain_parser.set_defaults(func=cluster_undrain_host)


def register_health_alias(subparsers):
    """Register top-level 'health' command as alias for 'cluster health'"""
    health_parser = subparsers.add_parser('health', help='Compact cluster health summary')
    health_parser.add_argument('-v', '--verbose', action='store_true', help='Show full detailed output')
    health_parser.set_defaults(func=cluster_health)


def cluster_health(args):
    """Execute compact cluster health check"""
    check_cluster.main(verbose=getattr(args, 'verbose', False))


def cluster_status_verbose(args):
    """Execute verbose cluster status check"""
    check_cluster.main(verbose=True)


def cluster_populate_local_node(args):
    """Populate local node information in etcd"""
    from ..utils import populate_local_node
    populate_local_node.populate_local_node()


def _set_host_state(hostname, action):
    """Helper to enable/disable a host (direct etcd write, CLI-only mutation)"""
    import sys
    from ..utils.host_state import set_host_disabled
    try:
        set_host_disabled(hostname, action == 'disable')
        print(f"{action.title()}d host: {hostname}")
    except KeyError:
        print(f"Error: Host '{hostname}' not found", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cluster_disable_host(args):
    """Disable a host so it doesn't appear in status page"""
    _set_host_state(args.hostname, 'disable')


def cluster_enable_host(args):
    """Re-enable a host so it appears in status page"""
    _set_host_state(args.hostname, 'enable')


def _set_drain_state(hostname, drain):
    """Helper to drain/undrain a node (direct etcd write, CLI-only mutation)"""
    import sys
    from ..utils.host_state import set_drain
    try:
        set_drain(hostname, drain)
        print(f"{hostname}: {'drained' if drain else 'active'}")
    except KeyError:
        print(f"Error: Host '{hostname}' not found", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cluster_drain_host(args):
    """Drain a node to disable leader election"""
    _set_drain_state(args.hostname, drain=True)


def cluster_undrain_host(args):
    """Undrain a node to re-enable leader election"""
    _set_drain_state(args.hostname, drain=False)
