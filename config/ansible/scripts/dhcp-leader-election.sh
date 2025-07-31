#!/bin/bash
set -e

ETCD_ENDPOINTS="$1"
LEASE_TTL=30
RENEW_INTERVAL=10

if [ -z "$ETCD_ENDPOINTS" ]; then
    echo "Usage: $0 <etcd_endpoints>"
    echo "Example: $0 http://localhost:2379"
    exit 1
fi

LEADER_KEY="/cluster/leader/dhcp"
NODE_ID=$(hostname)
LOCK_FILE="/var/run/dhcp-leader.lock"
LEASE_FILE="/var/run/dhcp-lease.lease"

# Function to cleanup on exit
cleanup() {
    echo "Cleaning up DHCP leader election"
    if [ -f "$LEASE_FILE" ]; then
        LEASE_ID=$(cat "$LEASE_FILE")
        timeout 5s etcdctl --endpoints="$ETCD_ENDPOINTS" lease revoke "$LEASE_ID" || true
        rm -f "$LEASE_FILE"
    fi
    rm -f "$LOCK_FILE"
    
    # Stop dnsmasq if we were the leader
    stop_dhcp_service
    exit 0
}

trap cleanup SIGTERM SIGINT EXIT

# Function to attempt leadership
attempt_leadership() {
    # Create a lease
    LEASE_ID=$(timeout 5s etcdctl --endpoints="$ETCD_ENDPOINTS" lease grant $LEASE_TTL | grep "lease" | awk '{print $2}')
    
    if [ -z "$LEASE_ID" ]; then
        echo "Failed to create lease"
        return 1
    fi
    
    # Try to acquire leadership using txn with correct syntax
    res=`echo -e "create(\"$LEADER_KEY\") = \"0\"\n\nput \"$LEADER_KEY\" \"$NODE_ID\" --lease=$LEASE_ID\n\n" | timeout 5s etcdctl --endpoints="$ETCD_ENDPOINTS" txn | head -1`
    echo result: $res
    if [[ "$res" == "SUCCESS" ]];
    then
        echo "Acquired DHCP leadership"
        echo "$LEASE_ID" > "$LEASE_FILE"
        touch "$LOCK_FILE"
        return 0
    else
        # Failed to acquire, check who is the current leader
        CURRENT_LEADER=$(timeout 5s etcdctl --endpoints="$ETCD_ENDPOINTS" get "$LEADER_KEY" --print-value-only 2>/dev/null || echo "unknown")
        echo "DHCP leadership held by $CURRENT_LEADER"
        timeout 5s etcdctl --endpoints="$ETCD_ENDPOINTS" lease revoke "$LEASE_ID"
        return 1
    fi
}

# Function to renew lease
renew_lease() {
    if [ -f "$LEASE_FILE" ]; then
        LEASE_ID=$(cat "$LEASE_FILE")
        if timeout 5s etcdctl --endpoints="$ETCD_ENDPOINTS" lease keep-alive "$LEASE_ID" --once; then
            return 0
        else
            echo "Failed to renew lease, lost DHCP leadership"
            rm -f "$LEASE_FILE" "$LOCK_FILE"
            return 1
        fi
    fi
    return 1
}

# Function to restore DHCP leases from etcd
restore_dhcp_leases() {
    echo "Restoring DHCP leases from etcd"
    
    # Use the Python script to restore leases
    ETCD_HOSTS="${ETCD_ENDPOINTS//http:\/\//}" /usr/local/bin/dhcp-lease-script.py restore
}

# Function to start DHCP service
start_dhcp_service() {
    echo "Starting DHCP service as leader"
    
    # Restore leases first
    restore_dhcp_leases
    
    # Start dnsmasq
    echo "Starting dnsmasq..."
    systemctl start dnsmasq || {
        echo "Failed to start dnsmasq service"
    }
}

# Function to stop DHCP service
stop_dhcp_service() {
    echo "Stopping DHCP service, no longer leader"
    systemctl stop dnsmasq || true
}

# Main loop
IS_LEADER=false
while true; do
    if [ "$IS_LEADER" = "false" ]; then
        # Try to become leader
        if attempt_leadership; then
            IS_LEADER=true
            start_dhcp_service
        else
            # Not leader, ensure service is stopped
            stop_dhcp_service
            sleep 5
            continue
        fi
    else
        # We are leader, try to renew lease
        if ! renew_lease; then
            IS_LEADER=false
            stop_dhcp_service
            continue
        fi
    fi
    
    sleep $RENEW_INTERVAL
done
