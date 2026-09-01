"""Helpers for PXE autoinstall rendering."""

import os


def ubuntu_password():
    """Return the crypted ubuntu password, preferring the env override."""
    password = os.environ.get('UBUNTU_PASSWORD_HASH')
    if password:
        return password
    password = _password_from_shadow('/etc/shadow')
    if not password:
        raise Exception(
            "Cannot read ubuntu password from /etc/shadow - set "
            "UBUNTU_PASSWORD_HASH env var or check permissions")
    return password


def _password_from_shadow(path):
    try:
        with open(path) as f:
            for line in f:
                fields = line.strip().split(':')
                if fields[0] == 'ubuntu' and len(fields) > 1:
                    return fields[1]
    except (PermissionError, FileNotFoundError):
        return None
    return None


def ssh_key(path):
    with open(path) as f:
        return f.read().strip()


def render_user_data(template, allocation, interfaces, proxy_url, gateway_vip,
                     key_content, password):
    node_type = allocation['type']
    values = {
        'node_type': node_type,
        'hostname': allocation['hostname'],
        'ip_address': allocation['ip'],
        'amt_ip_address': allocation['amt_ip'],
        'cluster_interface': interfaces['cluster_interface'],
        'uplink_interface': interfaces['uplink_interface'],
        'amt_interface': interfaces['amt_interface'],
        'ssh_key_content': key_content,
        'ubuntu_password': password,
        'proxy_url': proxy_url,
        'gateway_vip': gateway_vip,
    }
    return template.render(**values)
