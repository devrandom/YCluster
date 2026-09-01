#!/usr/bin/env python3
"""
Cluster heartbeat to healthchecks.io
Called by storage leader to send periodic health status
"""

import os
import re
import sys
import json
import socket
import requests
from datetime import datetime

from ycluster.common.etcd_utils import get_etcd_client

# etcd key for healthchecks URL
HEALTHCHECKS_ETCD_KEY = '/cluster/healthchecks/url'

def get_healthchecks_url():
    """Get healthchecks URL from etcd"""
    try:
        client = get_etcd_client()
        result = client.get(HEALTHCHECKS_ETCD_KEY)
        if result[0]:
            return result[0].decode()
        return None
    except Exception as e:
        print(f"Failed to get healthchecks URL from etcd: {str(e)}")
        return None


def _node_health_summary(cluster_status):
    """Count node states and collect names needing attention."""
    summary = {
        'healthy': 0, 'unhealthy': 0, 'unreachable': 0,
        'unhealthy_names': [], 'unreachable_names': [],
    }
    for hostname, node_health in cluster_status.get('hostHealth', {}).items():
        status = node_health.get('overall') or node_health.get('status', '')
        if status == 'disabled':
            continue
        if status == 'healthy':
            summary['healthy'] += 1
        elif status in ['timeout', 'unreachable']:
            summary['unreachable'] += 1
            summary['unreachable_names'].append(hostname)
        elif _clock_skew_only(hostname, node_health):
            summary['healthy'] += 1
        else:
            summary['unhealthy'] += 1
            summary['unhealthy_names'].append(hostname)
    summary['total'] = (summary['healthy'] + summary['unhealthy'] +
                        summary['unreachable'])
    return summary


def _clock_skew_only(hostname, node_health):
    """True for non-core nodes whose only bad service is clock_skew.

    Remote NTP checks flap and aren't worth alerting on. Storage nodes (s*)
    need tight time sync for Ceph, so clock skew there still counts.
    """
    if re.match(r'^s\d+$', hostname):
        return False
    bad = [
        svc for svc, d in node_health.get('services', {}).items()
        if isinstance(d, dict)
        and d.get('status') in ('unhealthy', 'error', 'degraded')
    ]
    return bool(bad) and all(svc == 'clock_skew' for svc in bad)


def _failed_services(services):
    """Return strings like 'svc(status)' for failed service details."""
    return [
        f"{svc}({details['status']})"
        for svc, details in services.items()
        if isinstance(details, dict)
        and details.get('status') in ['unhealthy', 'error', 'degraded']
    ]


def _unreachable_details(cluster_status, hostnames):
    """Render per-host diagnostics for unreachable/timeout nodes."""
    details = {}
    for hostname in hostnames:
        node_health = cluster_status.get('hostHealth', {}).get(hostname, {})
        parts = []
        elapsed = node_health.get('elapsed_ms')
        exc = node_health.get('exception')
        err = node_health.get('error')
        if elapsed is not None:
            parts.append(f"{elapsed}ms")
        if exc:
            parts.append(exc)
        if err:
            parts.append(err)
        if parts:
            details[hostname] = '; '.join(parts)
    return details


def get_cluster_health():
    """Get comprehensive cluster health status"""
    health_data = {
        'timestamp': datetime.now().isoformat(),
        'hostname': socket.gethostname(),
        'services': {},
        'nodes': {},
        'overall': 'healthy'
    }

    try:
        response = requests.get('http://localhost:12724/api/health', timeout=10)
        if response.status_code in [200, 503]:
            local_health = response.json()
            health_data['services'] = local_health.get('services', {})
            health_data['overall'] = local_health.get('overall', 'unknown')
            health_data['storage_leader'] = local_health.get('storage_leader', False)
            health_data['dhcp_leader'] = local_health.get('dhcp_leader', False)
    except Exception as e:
        health_data['error'] = f"Failed to get local health: {str(e)}"
        health_data['overall'] = 'error'

    _add_cluster_status(health_data)
    return health_data


def _add_cluster_status(health_data):
    """Merge cluster-wide status into the heartbeat payload."""
    try:
        response = requests.get('http://localhost:12723/api/cluster-status',
                                timeout=30)
        if response.status_code != 200:
            return
        cluster_status = response.json()
    except Exception as e:
        health_data['cluster_error'] = f"Failed to get cluster status: {str(e)}"
        return

    summary = _node_health_summary(cluster_status)
    health_data['nodes'] = summary

    failed_by_node = {
        hostname: _failed_services(
            cluster_status.get('hostHealth', {}).get(hostname, {}).get('services', {}))
        for hostname in summary['unhealthy_names'] + summary['unreachable_names']
    }
    failed_by_node = {h: services for h, services in failed_by_node.items() if services}
    if failed_by_node:
        health_data['unhealthy_services_by_node'] = failed_by_node

    unreachable_details = _unreachable_details(
        cluster_status, summary['unreachable_names'])
    if unreachable_details:
        health_data['unreachable_details_by_node'] = unreachable_details

    health_data['local_services'] = cluster_status.get('hostHealth', {}).get(
        socket.gethostname(), {}).get('services', {})
    health_data['leadership'] = cluster_status.get('leadership', {})
    health_data['vip_status'] = cluster_status.get('vipStatus', {})
    cert_status = cluster_status.get('certificateStatus', {})
    if cert_status:
        health_data['certificate'] = {
            'status': cert_status.get('status'),
            'days_until_expiry': cert_status.get('details', {}).get('days_until_expiry')
        }

def determine_health_status(health_data):
    """Determine overall health status and exit code"""
    # Critical failures (exit code 2)
    critical_conditions = [
        health_data.get('overall') == 'unhealthy',
        health_data.get('nodes', {}).get('unreachable', 0) > 1,
        health_data.get('certificate', {}).get('status') in ['expired', 'critical'],
        'etcd' in health_data.get('services', {}) and 
            health_data['services']['etcd'].get('status') == 'unhealthy',
        'ceph' in health_data.get('services', {}) and 
            health_data['services']['ceph'].get('status') == 'unhealthy'
    ]
    
    # Warning conditions (exit code 1)
    warning_conditions = [
        health_data.get('overall') == 'degraded',
        health_data.get('nodes', {}).get('unhealthy', 0) > 0,
        health_data.get('nodes', {}).get('unreachable', 0) == 1,
        health_data.get('certificate', {}).get('status') == 'warning'
    ]
    
    if any(critical_conditions):
        return 2, 'critical'
    elif any(warning_conditions):
        return 1, 'warning'
    else:
        return 0, 'healthy'

def _node_message_lines(health_data):
    """Render cluster node health and per-node diagnostics."""
    lines = []
    nodes = health_data.get('nodes', {})
    if not nodes:
        return lines

    lines.append(f"Nodes: {nodes.get('healthy', 0)}/{nodes.get('total', 0)} healthy")
    if nodes.get('unhealthy_names'):
        lines.append(f"  Unhealthy: {', '.join(nodes['unhealthy_names'])}")
    if nodes.get('unreachable_names'):
        details = health_data.get('unreachable_details_by_node', {})
        rendered = [
            f"{h}({details[h]})" if h in details else h
            for h in nodes['unreachable_names']
        ]
        lines.append(f"  Unreachable: {', '.join(rendered)}")
    return lines


def _status_message_lines(health_data):
    """Render leadership, VIPs, and certificate age."""
    lines = []
    leadership = health_data.get('leadership', {})
    if leadership:
        lines.append(
            f"Leadership: Storage: {leadership.get('storage_leader', 'none')} "
            f"DHCP: {leadership.get('dhcp_leader', 'none')}")

    vip_status = health_data.get('vip_status', {})
    if vip_status:
        gateway_vip = vip_status.get('gateway_vip', {})
        storage_vip = vip_status.get('storage_vip', {})
        lines.append(
            f"VIPs: Gateway: {gateway_vip.get('master_hostname')}  "
            f"Storage: {storage_vip.get('master_hostname')}")

    cert = health_data.get('certificate', {})
    if cert and cert.get('days_until_expiry') is not None:
        lines.append(f"Certificate expires in {cert['days_until_expiry']} days")
    return lines


def _service_message_lines(health_data):
    """Render unhealthy and degraded service names."""
    lines = []
    unhealthy_services = [
        service
        for service, details in health_data.get('services', {}).items()
        if isinstance(details, dict)
        and details.get('status') in ['unhealthy', 'error']
    ]
    if unhealthy_services:
        lines.append(f"Unhealthy Services: {', '.join(unhealthy_services)}")

    failed_services = []
    for service, details in health_data.get('local_services', {}).items():
        if not isinstance(details, dict):
            continue
        svc_status = details.get('status', '')
        if svc_status in ['unhealthy', 'error', 'degraded']:
            failed_services.append(f"{service}({svc_status})")
    if failed_services:
        lines.append(f"  Failed: {', '.join(failed_services)}")
    return lines


def format_health_message(health_data, status):
    """Format health data into a human-readable message"""
    lines = [
        f"Cluster Status: {status.upper()}",
        f"Time: {health_data['timestamp']}",
        f"Reporting Node: {health_data['hostname']}",
    ]
    lines.extend(_node_message_lines(health_data))

    for hostname, services in health_data.get(
            'unhealthy_services_by_node', {}).items():
        lines.append(f"  {hostname}: {', '.join(services)}")

    lines.extend(_status_message_lines(health_data))
    lines.extend(_service_message_lines(health_data))

    if 'error' in health_data:
        lines.append(f"\nError: {health_data['error']}")
    if 'cluster_error' in health_data:
        lines.append(f"\nCluster Error: {health_data['cluster_error']}")

    return '\n'.join(lines)

def send_heartbeat():
    """Send heartbeat to healthchecks.io"""
    # Get healthchecks URL from etcd
    healthchecks_url = get_healthchecks_url()
    if not healthchecks_url:
        print("No healthchecks URL configured in etcd")
        return False
    
    try:
        # Get health data
        health_data = get_cluster_health()
        
        # Determine status
        exit_code, status = determine_health_status(health_data)
        
        # Format message
        message = format_health_message(health_data, status)
        print(message)
        
        # Send appropriate ping. Healthchecks.io treats any non-zero exit
        # status as a failure, so warnings post to the bare URL (success)
        # with the WARNING body still visible in the ping log. /fail is
        # reserved for true critical conditions.
        if exit_code in (0, 1):
            response = requests.post(healthchecks_url,
                                    data=message,
                                    timeout=10)
        else:
            response = requests.post(f"{healthchecks_url}/fail",
                                    data=message,
                                    timeout=10)
        
        if response.status_code == 200:
            print(f"Heartbeat sent successfully (status: {status})")
            return True
        else:
            print(f"Heartbeat failed with HTTP {response.status_code}")
            return False
            
    except Exception as e:
        print(f"Failed to send heartbeat: {str(e)}")
        # Try to send failure notification
        try:
            requests.post(f"{healthchecks_url}/fail", 
                        data=f"Heartbeat script error: {str(e)}",
                        timeout=10)
        except:
            pass
        return False

def main():
    """Main function - single heartbeat execution"""
    # Send single heartbeat
    success = send_heartbeat()
    sys.exit(0 if success else 1)

if __name__ == '__main__':
    main()
