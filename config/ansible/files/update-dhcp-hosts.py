#!/usr/bin/env python3
"""
Update static DNS hosts from etcd
"""

import requests
import tempfile
import filecmp
import subprocess
import sys
import os

def main():
    """Update static DNS hosts from etcd"""
    hosts_file = "/etc/static-hosts"
    
    try:
        # Fetch hosts data with timeout and proper error handling
        response = requests.get("http://localhost:12723/api/hosts", timeout=10)
        response.raise_for_status()
        
        if not response.text.strip():
            print("Warning: Empty hosts data received from API")
            return
        
        # Write to temporary file
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as temp_file:
            temp_file.write(response.text)
            temp_file_path = temp_file.name
        
        try:
            # Only reload if the configuration has actually changed
            if not os.path.exists(hosts_file) or not filecmp.cmp(temp_file_path, hosts_file):
                print("DNS hosts configuration changed, updating and reloading dnsmasq")
                # Copy temp file to hosts file
                subprocess.run(['cp', temp_file_path, hosts_file], check=True)
                # Reload dnsmasq
                subprocess.run(['systemctl', 'reload', 'dnsmasq'], check=True)
            else:
                print("DNS hosts configuration unchanged")
        finally:
            # Clean up temp file
            os.unlink(temp_file_path)
            
    except requests.exceptions.Timeout:
        print("Warning: Timeout fetching hosts data from API")
        sys.exit(1)
    except requests.exceptions.RequestException as e:
        print(f"Warning: Failed to fetch hosts data from API: {e}")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"Error: Failed to update hosts or reload dnsmasq: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: Unexpected error: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()
