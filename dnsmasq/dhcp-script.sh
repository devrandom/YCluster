#!/bin/bash
# Called by dnsmasq for DHCP events
# Arguments: add|old|del MAC IP hostname

ACTION=$1
MAC=$2
IP=$3
HOSTNAME=$4

# Log for debugging
echo "$(date): DHCP script called with: $ACTION $MAC $IP $HOSTNAME" >> /var/log/dhcp-script.log

if [ "$ACTION" = "add" ] || [ "$ACTION" = "old" ]; then
    # Query httpd for the correct allocation
    RESPONSE=$(curl -s "http://10.0.0.1/api/allocate?mac=$MAC" 2>/dev/null)
    
    if [ $? -eq 0 ] && [ -n "$RESPONSE" ]; then
        # Parse the JSON response
        ASSIGNED_IP=$(echo "$RESPONSE" | grep -o '"ip":"[^"]*' | cut -d'"' -f4)
        ASSIGNED_HOSTNAME=$(echo "$RESPONSE" | grep -o '"hostname":"[^"]*' | cut -d'"' -f4)
        
        echo "$(date): Allocation response: IP=$ASSIGNED_IP, Hostname=$ASSIGNED_HOSTNAME" >> /var/log/dhcp-script.log
        
        # If we got a valid response and the IP differs from what dnsmasq wants to assign
        if [ -n "$ASSIGNED_IP" ] && [ "$ASSIGNED_IP" != "$IP" ]; then
            echo "$(date): IP mismatch! DHCP wants $IP but allocation says $ASSIGNED_IP" >> /var/log/dhcp-script.log
            
            # Regenerate the entire dynamic hosts file from the allocation API
            STATIC_FILE="/etc/dnsmasq.d/dynamic-hosts.conf"
            
            # Fetch the complete DHCP configuration
            curl -s "http://10.0.0.1/api/dhcp-config" > "$STATIC_FILE.tmp" 2>/dev/null
            
            if [ $? -eq 0 ] && [ -s "$STATIC_FILE.tmp" ]; then
                mv "$STATIC_FILE.tmp" "$STATIC_FILE"
                
                # Signal dnsmasq to reload
                pkill -HUP dnsmasq
                
                echo "$(date): Regenerated static mappings file" >> /var/log/dhcp-script.log
            else
                echo "$(date): Failed to regenerate static mappings" >> /var/log/dhcp-script.log
                rm -f "$STATIC_FILE.tmp"
            fi
        fi
    else
        echo "$(date): Failed to query allocation API" >> /var/log/dhcp-script.log
    fi
fi
