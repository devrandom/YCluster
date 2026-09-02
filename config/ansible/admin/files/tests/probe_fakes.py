"""Shared fake probe contexts for the lite health API tests."""

import health_common as hc


def make_probe(*, services=(), ports=(), responses=None, etcd_client=None,
               is_etcd_node=True, core_node=True, http_get=None):
    """Build a probe context with explicit fake data."""
    services = set(services)
    ports = set(ports)
    return hc.HealthProbeContext(
        is_etcd_node=is_etcd_node,
        is_storage_node=True,
        etcd_client=lambda: etcd_client,
        service_status=lambda service: service in services,
        port_open=lambda host, port, timeout=3: port in ports,
        http_get=http_get or ((lambda url, timeout=5, **kwargs: responses[url]) if responses else (lambda *args, **kwargs: None)),
        storage_leader=lambda: True,
        dhcp_leader=lambda: False,
        node_drained=lambda: False,
        core_node=lambda: core_node,
    )
