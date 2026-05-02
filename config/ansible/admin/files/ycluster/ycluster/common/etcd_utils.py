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
    last_errors = []
    
    for attempt in range(max_retries):
        attempt_errors = []
        for host_port in hosts:
            try:
                host, port = host_port.split(':')
                client = etcd3.client(host=host, port=int(port), grpc_options=grpc_options)
                client.status()
                return client
            except Exception as e:
                attempt_errors.append(f"{host_port}: {str(e)}")
                continue
        
        last_errors = attempt_errors
        if attempt < max_retries - 1:
            time.sleep(retry_delay)
    
    error_details = "; ".join(last_errors)
    raise ConnectionError(f"Could not connect to any etcd host after {max_retries} attempts. Errors: {error_details}")


_CACHED_CLIENT = None


def get_etcd_client(hosts=None, max_retries=3, retry_delay=1, grpc_options=None):
    """Return a cached etcd client, creating it with retries if needed.

    The cached client is reused without a round-trip status() call —
    status() costs ~330ms per call on some nodes, and calling it on every
    get_etcd_client() invocation adds up to seconds when the health
    endpoint calls get_etcd_client() many times per request. If the
    cached client has gone stale, the next real operation will raise an
    exception; callers that need reconnect behaviour should set
    _CACHED_CLIENT = None and retry.
    """
    global _CACHED_CLIENT
    if _CACHED_CLIENT is not None:
        return _CACHED_CLIENT

    hosts = hosts or get_etcd_hosts()
    try:
        _CACHED_CLIENT = connect_with_retry(
            hosts, max_retries=max_retries, retry_delay=retry_delay, grpc_options=grpc_options
        )
        return _CACHED_CLIENT
    except ConnectionError as e:
        print(f"Failed to establish etcd connection: {e}")
        raise
