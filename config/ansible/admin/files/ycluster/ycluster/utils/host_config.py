"""Per-host configuration reads from /etc/ycluster/host.yml.

Line-based parsing on purpose: it tolerates malformed lines and avoids
importing yaml in the CLI hot path. Keep the semantics identical to the
historical vm_manager implementation.
"""

HOST_CONFIG_PATH = "/etc/ycluster/host.yml"


def host_config_value(wanted):
    """Read one key from /etc/ycluster/host.yml. None when absent."""
    try:
        with open(HOST_CONFIG_PATH) as f:
            for line in f:
                key, _, val = line.partition(":")
                if key.strip() == wanted:
                    return val.strip().strip('"').strip("'")
    except OSError:
        pass
    return None
