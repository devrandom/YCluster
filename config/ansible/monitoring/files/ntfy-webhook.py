#!/usr/bin/env python3
"""
Alertmanager webhook handler for ntfy notifications.
Receives Alertmanager webhook payloads and forwards them to ntfy with proper formatting.
"""

import json
import logging
import requests
from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
NTFY_TOPIC = "cf0d6065-2357-493e-a7a8-06666dd660eb"
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"
PROMETHEUS_URL = "http://localhost:9090"

def query_blackbox_nodes(domain, instance):
    """Query Prometheus for blackbox_node labels for a given domain/instance."""
    try:
        # Query for all blackbox nodes that have probed this target
        query = f'probe_success{{job="https-domain",domain="{domain}",instance="{instance}"}}'
        response = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={'query': query},
            timeout=5
        )
        response.raise_for_status()
        
        data = response.json()
        if data.get('status') == 'success' and data.get('data', {}).get('result'):
            blackbox_nodes = []
            for result in data['data']['result']:
                blackbox_node = result.get('metric', {}).get('blackbox_node')
                if blackbox_node:
                    blackbox_nodes.append(blackbox_node)
            return blackbox_nodes
        return []
    except Exception as e:
        logger.error(f"Failed to query Prometheus for blackbox nodes: {e}")
        return []

def format_alert_message(alert):
    """Format a single alert for ntfy."""
    status = alert.get('status', 'unknown')
    labels = alert.get('labels', {})
    annotations = alert.get('annotations', {})
    
    alertname = labels.get('alertname', 'Unknown Alert')
    severity = labels.get('severity', 'info')
    node = labels.get('node') or None
    
    summary = annotations.get('summary', f'{alertname}' + (f' on {node}' if node else ''))
    description = annotations.get('description', 'No description available')
    
    # Format timestamp - use endsAt for resolved, startsAt for firing
    if status == 'resolved':
        timestamp_field = alert.get('endsAt', '')
    else:
        timestamp_field = alert.get('startsAt', '')
    
    if timestamp_field:
        try:
            dt = datetime.fromisoformat(timestamp_field.replace('Z', '+00:00'))
            timestamp = dt.strftime('%Y-%m-%d %H:%M:%S UTC')
        except:
            timestamp = timestamp_field
    else:
        timestamp = 'Unknown time'
    
    # Check if this alert has domain/instance labels (indicates blackbox monitoring)
    domain = labels.get('domain')
    instance = labels.get('instance')
    blackbox_info = ""
    
    if domain and instance:
        blackbox_nodes = query_blackbox_nodes(domain, instance)
        if blackbox_nodes:
            blackbox_info = f"\nBlackbox nodes: {', '.join(blackbox_nodes)}"
        else:
            blackbox_info = "\nBlackbox nodes: (query failed or no data)"
    
    if status == 'resolved':
        title = f"[RESOLVED] {summary}"
        message = f"Alert resolved at {timestamp}\n\n{description}{blackbox_info}"
        priority = "default"
        tags = "white_check_mark"
    else:
        if severity == 'critical':
            title = f"[CRITICAL] {summary}"
            priority = "urgent"
            tags = "rotating_light"
        elif severity == 'warning':
            title = f"[WARNING] {summary}"
            priority = "default"
            tags = "warning"
        else:
            title = f"[INFO] {summary}"
            priority = "low"
            tags = "information_source"
        
        # Build message with optional node line
        message_parts = [f"Alert started at {timestamp}"]
        if node is not None:
            message_parts.append(f"Node: {node}")
        message_parts.append(f"Severity: {severity}")
        message_parts.append("")
        message_parts.append(description)
        if blackbox_info:
            message_parts.append(blackbox_info)
        
        message = "\n".join(message_parts)
    
    return {
        'title': title,
        'message': message,
        'priority': priority,
        'tags': tags
    }

def send_to_ntfy(title, message, priority="default", tags=""):
    """Send notification to ntfy."""
    headers = {
        'Title': title.encode('utf-8').decode('utf-8'),
        'Priority': priority,
        'Tags': tags,
        'Content-Type': 'text/plain; charset=utf-8'
    }
    
    try:
        # Ensure message is properly encoded as UTF-8
        message_bytes = message.encode('utf-8')
        response = requests.post(NTFY_URL, data=message_bytes, headers=headers, timeout=10)
        response.raise_for_status()
        logger.info(f"Sent notification to ntfy: {title}")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to send notification to ntfy: {e}")
        return False

@app.route('/webhook', methods=['POST'])
def webhook():
    """Handle Alertmanager webhook."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No JSON data received'}), 400
        
        logger.info(f"Received webhook with data: {json.dumps(data, indent=2)}")

        alerts = data.get('alerts', [])
        success_count = 0
        
        for alert in alerts:
            try:
                formatted = format_alert_message(alert)
                if send_to_ntfy(
                    formatted['title'],
                    formatted['message'],
                    formatted['priority'],
                    formatted['tags']
                ):
                    success_count += 1
            except Exception as e:
                logger.error(f"Error processing alert: {e}")
        
        # Return error if we couldn't send any notifications
        if len(alerts) > 0 and success_count == 0:
            return jsonify({
                'error': 'Failed to send any notifications',
                'processed': len(alerts),
                'sent': 0
            }), 503  # Service Unavailable - triggers Alertmanager retry

        # TODO consider if it's correct to return 200 if some alerts failed

        return jsonify({
            'status': 'success',
            'processed': len(alerts),
            'sent': success_count
        })
        
    except Exception as e:
        logger.error(f"Error handling webhook: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    return jsonify({'status': 'healthy', 'service': 'ntfy-webhook'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9095, debug=False)
