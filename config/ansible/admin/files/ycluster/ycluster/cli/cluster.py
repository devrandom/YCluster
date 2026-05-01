"""
Cluster status and health management commands
"""

from ..utils import check_cluster
import requests


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
    """Helper to enable/disable a host via API"""
    import sys
    storage_leader = get_storage_leader_ip()
    if not storage_leader:
        print("Error: Could not determine storage leader", file=sys.stderr)
        sys.exit(1)

    url = f"http://{storage_leader}:12723/api/host/{hostname}/{action}"
    try:
        response = requests.post(url, timeout=10)
        if response.status_code == 200:
            print(f"{action.title()}d host: {hostname}")
        elif response.status_code == 404:
            print(f"Error: Host '{hostname}' not found", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"Error: {response.json().get('error', 'Unknown error')}", file=sys.stderr)
            sys.exit(1)
    except requests.exceptions.RequestException as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cluster_disable_host(args):
    """Disable a host so it doesn't appear in status page"""
    _set_host_state(args.hostname, 'disable')


def cluster_enable_host(args):
    """Re-enable a host so it appears in status page"""
    _set_host_state(args.hostname, 'enable')


def _set_drain_state(hostname, drain):
    """Helper to drain/undrain a node via the admin API"""
    import sys
    storage_leader = get_storage_leader_ip()
    if not storage_leader:
        print("Error: Could not determine storage leader", file=sys.stderr)
        sys.exit(1)

    action = 'drain' if drain else 'undrain'
    url = f"http://{storage_leader}:12723/api/{action}/{hostname}"
    try:
        response = requests.post(url, timeout=10)
        data = response.json()
        if response.status_code == 200:
            status = data.get('status', action + 'ed')
            print(f"{hostname}: {status}")
        else:
            print(f"Error: {data.get('error', 'Unknown error')}", file=sys.stderr)
            sys.exit(1)
    except requests.exceptions.RequestException as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cluster_drain_host(args):
    """Drain a node to disable leader election"""
    _set_drain_state(args.hostname, drain=True)


def cluster_undrain_host(args):
    """Undrain a node to re-enable leader election"""
    _set_drain_state(args.hostname, drain=False)


def get_storage_leader_ip():
    """Get storage leader IP from etcd"""
    from ..common.etcd_utils import get_etcd_client
    import json
    try:
        client = get_etcd_client()
        result = client.get('/cluster/leader/app')
        if not result[0]:
            return None
        leader_hostname = result[0].decode()
        result = client.get(f'/cluster/nodes/by-hostname/{leader_hostname}')
        if result[0]:
            allocation = json.loads(result[0].decode())
            return allocation.get('ip')
    except:
        pass
    return None
