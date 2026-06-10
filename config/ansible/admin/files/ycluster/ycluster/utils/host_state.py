"""
Host state mutations (disable/enable, leader-election drain).

These write etcd directly: cluster mutations are CLI-only, authenticated
by the cluster-CA etcd client certificate rather than an HTTP endpoint.
"""

import json
from ..common.etcd_utils import get_etcd_client

ETCD_PREFIX = '/cluster/nodes'


def _get_allocation(client, hostname):
    result = client.get(f"{ETCD_PREFIX}/by-hostname/{hostname}")
    if not result[0]:
        raise KeyError(hostname)
    return json.loads(result[0].decode())


def set_host_disabled(hostname, disabled):
    """Set the disabled flag on a host allocation; returns the allocation.

    Raises KeyError if the hostname has no allocation.
    """
    client = get_etcd_client()
    allocation = _get_allocation(client, hostname)
    allocation['disabled'] = disabled
    payload = json.dumps(allocation)
    client.put(f"{ETCD_PREFIX}/by-hostname/{hostname}", payload)
    client.put(f"{ETCD_PREFIX}/by-mac/{allocation['mac']}", payload)
    return allocation


def set_drain(hostname, drain):
    """Set or clear the leader-election drain flag for a node.

    Raises KeyError if the hostname has no allocation.
    """
    client = get_etcd_client()
    _get_allocation(client, hostname)
    if drain:
        client.put(f"{ETCD_PREFIX}/{hostname}/drain", 'true')
    else:
        client.delete(f"{ETCD_PREFIX}/{hostname}/drain")
