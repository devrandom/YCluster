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

LEADER_KEY="/cluster/leader/app"
NODE_ID=$(hostname)
LOCK_FILE="/var/run/etcd-leader.lock"
LEASE_FILE="/var/run/etcd-lease.lease"

# Function to cleanup on exit
cleanup() {
    echo "Cleaning up storage leader election"
    if [ -f "$LEASE_FILE" ]; then
        LEASE_ID=$(cat "$LEASE_FILE")
        timeout 5s etcdctl --endpoints="$ETCD_ENDPOINTS" lease revoke "$LEASE_ID" || true
        rm -f "$LEASE_FILE"
    fi
    rm -f "$LOCK_FILE"
    
    # Stop all services if we were the leader
    stop_all_services
    exit 0
}

trap cleanup SIGTERM SIGINT EXIT

# Wait for etcd endpoints to be reachable before attempting election
wait_for_etcd() {
    local attempts=0
    local max_attempts=12
    local IFS=','

    while [ $attempts -lt $max_attempts ]; do
        for ep in $ETCD_ENDPOINTS; do
            if timeout 3s etcdctl --endpoints="$ep" endpoint health >/dev/null 2>&1; then
                return 0
            fi
        done
        attempts=$((attempts + 1))
        sleep 2
    done
    echo "Warning: etcd health check failed, proceeding anyway"
}

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
    if [[ "$res" == "SUCCESS" ]];
    then
        echo "Acquired storage leadership"
        echo "$LEASE_ID" > "$LEASE_FILE"
        touch "$LOCK_FILE"
        return 0
    else
        timeout 5s etcdctl --endpoints="$ETCD_ENDPOINTS" lease revoke "$LEASE_ID" >/dev/null 2>&1
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
            echo "Failed to renew lease, lost storage leadership"
            rm -f "$LEASE_FILE" "$LOCK_FILE"
            return 1
        fi
    fi
    return 1
}

# Function to start all services
start_all_services() {
    echo "Starting all services as storage leader"
    
    # Start PostgreSQL
    systemctl start postgres-rbd || {
        echo "Failed to start postgres-rbd service"
    }
    
    # Start Qdrant
    systemctl start qdrant-rbd || {
        echo "Failed to start qdrant-rbd service"
    }
}

# Function to stop all services
stop_all_services() {
    echo "Stopping all services, no longer storage leader"
    systemctl stop postgres-rbd >/dev/null 2>&1 || true
    systemctl stop qdrant-rbd >/dev/null 2>&1 || true
}

# Main loop
IS_LEADER=false
SERVICES_RUNNING=false
while true; do
    wait_for_etcd
    if [ "$IS_LEADER" = "false" ]; then
        # Try to become leader
        if attempt_leadership; then
            IS_LEADER=true
            if [ "$SERVICES_RUNNING" = "false" ]; then
                start_all_services
                SERVICES_RUNNING=true
            fi
        else
            # Not leader, ensure services are stopped
            if [ "$SERVICES_RUNNING" = "true" ]; then
                stop_all_services
                SERVICES_RUNNING=false
            fi
            sleep 5
            continue
        fi
    else
        # We are leader, try to renew lease
        if ! renew_lease; then
            IS_LEADER=false
            if [ "$SERVICES_RUNNING" = "true" ]; then
                stop_all_services
                SERVICES_RUNNING=false
            fi
            continue
        fi
    fi
    
    sleep $RENEW_INTERVAL
done
