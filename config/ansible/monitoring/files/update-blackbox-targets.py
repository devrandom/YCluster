#!/usr/bin/env python3
"""
Update Blackbox Exporter targets using file-based service discovery
Only creates targets if node has internet connectivity (default route)
Also writes uplink status metrics for node exporter textfile collector
"""

import os
import sys
import json
import tempfile
import shutil
import subprocess

# Add the ycluster module path
sys.path.insert(0, '/usr/local/lib/ycluster')

from ycluster.common.etcd_utils import get_etcd_client

TARGETS_FILE = '/etc/prometheus/blackbox-targets.json'
TEXTFILE_DIR = '/var/lib/prometheus/node-exporter'
METRICS_FILE = f'{TEXTFILE_DIR}/uplink.prom'

def has_internet_uplink():
    """Check if node has internet connectivity by checking for default route"""
    try:
        # Check for default route using JSON output
        result = subprocess.run(['ip', '-j', 'route', 'show', 'default'], 
                              capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            routes = json.loads(result.stdout)
            if routes:
                print(f"Default route found: {routes[0].get('dev', 'unknown interface')}")
                return True
            else:
                print("No default route found")
                return False
        else:
            print(f"Failed to get route information: {result.stderr}")
            return False
    except Exception as e:
        print(f"Failed to check default route: {e}")
        return False

def get_targets_count():
    """Get current number of blackbox targets"""
    try:
        if os.path.exists(TARGETS_FILE):
            with open(TARGETS_FILE, 'r') as f:
                targets = json.load(f)
                return len(targets)
        return 0
    except:
        return 0

def write_uplink_metrics():
    """Write uplink metrics to textfile for node exporter"""
    try:
        uplink_status = 1 if has_internet_uplink() else 0
        targets_count = get_targets_count()
        
        metrics_content = f"""# HELP ycluster_uplink_status Internet uplink availability (1=available, 0=unavailable)
# TYPE ycluster_uplink_status gauge
ycluster_uplink_status{{node="{os.uname().nodename}"}} {uplink_status}

# HELP ycluster_blackbox_targets_count Number of blackbox targets configured
# TYPE ycluster_blackbox_targets_count gauge
ycluster_blackbox_targets_count{{node="{os.uname().nodename}"}} {targets_count}
"""
        
        # Write atomically using temp file
        with tempfile.NamedTemporaryFile(mode='w', delete=False, 
                                       dir=TEXTFILE_DIR, suffix='.prom') as tmp:
            tmp.write(metrics_content)
            tmp_path = tmp.name
        
        shutil.move(tmp_path, METRICS_FILE)
        
        # Set correct ownership and permissions
        shutil.chown(METRICS_FILE, user='prometheus', group='prometheus')
        os.chmod(METRICS_FILE, 0o644)
        
        print(f"Updated uplink metrics: uplink={uplink_status}, targets={targets_count}")
        
    except Exception as e:
        print(f"Failed to write uplink metrics: {e}")

def get_https_domain():
    """Get HTTPS domain from etcd"""
    try:
        client = get_etcd_client()
        result = client.get('/cluster/https/domain')
        if result[0]:
            return result[0].decode().strip()
        return None
    except Exception as e:
        print(f"Failed to get HTTPS domain from etcd: {e}")
        return None

def update_blackbox_targets():
    """Update Blackbox targets file"""
    domain = get_https_domain()

    # Only create targets if we have internet connectivity
    if not has_internet_uplink():
        print("No internet uplink detected, clearing blackbox targets")
        targets = []
    else:
        if domain:
            targets = [{
                "targets": [f"https://{domain}"],
                "labels": {
                    "job": "https-domain",
                    "domain": domain
                }
            }]
        else:
            targets = []
    
    try:
        # Write to temp file first
        with tempfile.NamedTemporaryFile(mode='w', delete=False, 
                                       dir=os.path.dirname(TARGETS_FILE)) as tmp:
            json.dump(targets, tmp, indent=2)
            tmp_path = tmp.name
        
        # Atomic move
        shutil.move(tmp_path, TARGETS_FILE)
        
        # Set correct ownership and permissions
        shutil.chown(TARGETS_FILE, user='prometheus', group='prometheus')
        os.chmod(TARGETS_FILE, 0o644)
        
        if not has_internet_uplink():
            print("Blackbox targets cleared (no internet uplink)")
        elif domain:
            print(f"Updated blackbox targets for domain: {domain}")
        else:
            print("Cleared blackbox targets (no domain configured)")
        
        # Write uplink metrics after updating targets
        write_uplink_metrics()
        
        return True
        
    except Exception as e:
        print(f"Failed to update blackbox targets: {e}")
        return False

def main():
    """Main function"""
    if update_blackbox_targets():
        print("Blackbox targets updated successfully")
    return 0

if __name__ == '__main__':
    sys.exit(main())
