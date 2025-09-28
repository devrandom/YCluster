#!/usr/bin/env python3
"""
Cluster heartbeat to healthchecks.io
Called by storage leader to send periodic health status
"""

import os
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
        # Check local health API
        response = requests.get('http://localhost:12723/api/health', timeout=10)
        if response.status_code in [200, 503]:
            local_health = response.json()
            health_data['services'] = local_health.get('services', {})
            health_data['overall'] = local_health.get('overall', 'unknown')
            health_data['storage_leader'] = local_health.get('storage_leader', False)
            health_data['dhcp_leader'] = local_health.get('dhcp_leader', False)
    except Exception as e:
        health_data['error'] = f"Failed to get local health: {str(e)}"
        health_data['overall'] = 'error'
    
    # Get cluster-wide status
    try:
        response = requests.get('http://localhost:12723/api/cluster-status', timeout=30)
        if response.status_code == 200:
            cluster_status = response.json()
            
            # Count healthy vs unhealthy nodes
            healthy_nodes = 0
            unhealthy_nodes = 0
            unreachable_nodes = 0
            
            for hostname, node_health in cluster_status.get('hostHealth', {}).items():
                if node_health.get('overall') == 'healthy':
                    healthy_nodes += 1
                elif node_health.get('overall') in ['timeout', 'unreachable']:
                    unreachable_nodes += 1
                else:
                    unhealthy_nodes += 1
            
            health_data['nodes'] = {
                'healthy': healthy_nodes,
                'unhealthy': unhealthy_nodes,
                'unreachable': unreachable_nodes,
                'total': healthy_nodes + unhealthy_nodes + unreachable_nodes
            }
            
            # Add leadership info
            health_data['leadership'] = cluster_status.get('leadership', {})
            
            # Add VIP status
            health_data['vip_status'] = cluster_status.get('vipStatus', {})
            
            # Add certificate status
            cert_status = cluster_status.get('certificateStatus', {})
            if cert_status:
                health_data['certificate'] = {
                    'status': cert_status.get('status'),
                    'days_until_expiry': cert_status.get('details', {}).get('days_until_expiry')
                }
    except Exception as e:
        health_data['cluster_error'] = f"Failed to get cluster status: {str(e)}"
    
    
    return health_data

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

def format_health_message(health_data, status):
    """Format health data into a human-readable message"""
    lines = []
    lines.append(f"Cluster Status: {status.upper()}")
    lines.append(f"Time: {health_data['timestamp']}")
    lines.append(f"Reporting Node: {health_data['hostname']}")
    
    # Node status
    nodes = health_data.get('nodes', {})
    if nodes:
        lines.append(f"\nNodes: {nodes.get('healthy', 0)}/{nodes.get('total', 0)} healthy")
        if nodes.get('unhealthy', 0) > 0:
            lines.append(f"  Unhealthy: {nodes['unhealthy']}")
        if nodes.get('unreachable', 0) > 0:
            lines.append(f"  Unreachable: {nodes['unreachable']}")
    
    # Leadership
    leadership = health_data.get('leadership', {})
    if leadership:
        lines.append(f"\nLeadership:")
        lines.append(f"  Storage: {leadership.get('storage_leader', 'none')}")
        lines.append(f"  DHCP: {leadership.get('dhcp_leader', 'none')}")
    
    # VIP status
    vip_status = health_data.get('vip_status', {})
    if vip_status:
        gateway_vip = vip_status.get('gateway_vip', {})
        storage_vip = vip_status.get('storage_vip', {})
        if gateway_vip.get('master_hostname'):
            lines.append(f"\nVIPs:")
            lines.append(f"  Gateway (10.0.0.254): {gateway_vip['master_hostname']}")
        if storage_vip.get('master_hostname'):
            lines.append(f"  Storage (10.0.0.100): {storage_vip['master_hostname']}")
    
    # Certificate
    cert = health_data.get('certificate', {})
    if cert and cert.get('days_until_expiry') is not None:
        lines.append(f"\nCertificate expires in {cert['days_until_expiry']} days")
    
    # Critical service issues
    services = health_data.get('services', {})
    unhealthy_services = []
    for service, details in services.items():
        if isinstance(details, dict) and details.get('status') in ['unhealthy', 'error']:
            unhealthy_services.append(service)
    
    if unhealthy_services:
        lines.append(f"\nUnhealthy Services: {', '.join(unhealthy_services)}")
    
    # Errors
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
        
        # Send appropriate ping
        if exit_code == 0:
            # Success ping
            response = requests.post(healthchecks_url, 
                                    data=message,
                                    timeout=10)
        elif exit_code == 1:
            # Warning - use exit code endpoint
            response = requests.post(f"{healthchecks_url}/1", 
                                    data=message,
                                    timeout=10)
        else:
            # Failure
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
