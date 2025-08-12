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
    
    # Start PostgreSQL in background
    systemctl start postgres-rbd &
    PG_START_PID=$!
    
    # Start Qdrant in background  
    systemctl start qdrant-rbd &
    QDRANT_START_PID=$!
    
    echo "Services starting in background (PostgreSQL PID: $PG_START_PID, Qdrant PID: $QDRANT_START_PID)"
}

# Function to stop all services
stop_all_services() {
    echo "Lost storage leadership - attempting graceful shutdown first"
    
    # Try graceful shutdown first with timeout
    echo "Attempting graceful service shutdown (5 second timeout)"
    timeout 5s systemctl stop postgres-rbd &
    timeout 5s systemctl stop qdrant-rbd &
    wait
    
    # Check if services are still running and force cleanup if needed
    if pgrep -f postgres >/dev/null || pgrep -f qdrant >/dev/null; then
        echo "Graceful shutdown failed or timed out - forcing aggressive cleanup"
        
        # Force kill processes immediately
        echo "Force killing PostgreSQL processes"
        pkill -9 postgres || true
        
        echo "Force killing Qdrant processes"
        pkill -9 qdrant || true
        
        # Force XFS shutdown to abandon all I/O immediately (only if mounted)
        echo "Force shutting down XFS filesystems"
        if mountpoint -q /rbd/pg; then
            xfs_io -x -c "shutdown" /rbd/pg 2>/dev/null || true
        fi
        if mountpoint -q /rbd/qdrant; then
            xfs_io -x -c "shutdown" /rbd/qdrant 2>/dev/null || true
        fi
        
        # Force unmount filesystems (in parallel)
        echo "Force unmounting RBD filesystems"
        umount -f -l /rbd/pg &
        umount -f -l /rbd/qdrant &
        wait
        
        # Force unmap RBDs (in parallel with timeout)
        echo "Force unmapping RBD devices"
        timeout 10s rbd unmap -o force /dev/rbd/rbd/psql &
        timeout 10s rbd unmap -o force /dev/rbd/rbd/qdrant &
        wait
        
        echo "Aggressive cleanup completed"
    else
        echo "Graceful shutdown successful"
    fi
    
    # Clean up lock files
    echo "Cleaning up lock files"
    rm -f /var/run/postgres-rbd.lock || true
    rm -f /var/run/qdrant-rbd.lock || true
}

# Main loop
IS_LEADER=false
while true; do
    if [ "$IS_LEADER" = "false" ]; then
        # Try to become leader
        if attempt_leadership; then
            IS_LEADER=true
            start_all_services
        fi
    else
        # We are leader, try to renew lease
        if ! renew_lease; then
            IS_LEADER=false
            stop_all_services
        fi
    fi
    
    sleep $RENEW_INTERVAL
done
