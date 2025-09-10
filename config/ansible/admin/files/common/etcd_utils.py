import os
import time
import etcd3


def get_etcd_hosts():
    """Return etcd hosts from environment.

    Checks ETCD_HOSTS (comma separated) then ETCD_HOST.
    Falls back to localhost if nothing is set.
    """
    hosts = os.environ.get('ETCD_HOSTS')
    if hosts:
        host_list = [h.strip() for h in hosts.split(',') if h.strip()]
        if host_list:
            return host_list
    host = os.environ.get('ETCD_HOST')
    if host:
        return [host.strip()]
    return ['localhost:2379']


def connect_with_retry(hosts, max_retries=3, retry_delay=1, grpc_options=None):
    """Attempt to connect to etcd using host list with retries."""
    grpc_options = grpc_options or [('grpc.enable_http_proxy', 0)]
    for _ in range(max_retries):
        for host_port in hosts:
            try:
                host, port = host_port.split(':')
                client = etcd3.client(host=host, port=int(port), grpc_options=grpc_options)
                client.status()
                return client
            except Exception:
                continue
        time.sleep(retry_delay)
    raise ConnectionError(f"Could not connect to any etcd host: {hosts}")


_CACHED_CLIENT = None


def get_etcd_client(hosts=None, max_retries=3, retry_delay=1, grpc_options=None):
    """Return a cached etcd client, creating it with retries if needed."""
    global _CACHED_CLIENT
    if _CACHED_CLIENT:
        try:
            _CACHED_CLIENT.status()
            return _CACHED_CLIENT
        except Exception:
            _CACHED_CLIENT = None

    hosts = hosts or get_etcd_hosts()
    _CACHED_CLIENT = connect_with_retry(
        hosts, max_retries=max_retries, retry_delay=retry_delay, grpc_options=grpc_options
    )
    return _CACHED_CLIENT
