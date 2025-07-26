from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route('/api/allocate')
def allocate_hostname():
    """Always allocate s1 - static bootstrap node"""
    mac_address = request.args.get('mac', 'unknown')
    
    return jsonify({
        'hostname': 's1',
        'type': 'storage',
        'ip': '10.0.0.11',
        'mac': mac_address,
        'existing': False
    })

@app.route('/api/status')
def status():
    """Get current status - static s1 only"""
    return jsonify({'storage': 1})

@app.route('/api/allocations')
def allocations():
    """Get all current allocations - static s1 only"""
    return jsonify([{
        'mac': 'unknown',
        'hostname': 's1',
        'type': 'storage',
        'ip': '10.0.0.11',
        'allocated_at': 'static'
    }])

@app.route('/api/dhcp-config')
def get_dhcp_config():
    """Get static DHCP configuration for s1"""
    # Note: We don't know the MAC address yet, so this will be empty initially
    # The DHCP script will handle dynamic assignment
    return "", 200, {'Content-Type': 'text/plain'}

@app.route('/api/health')
def health():
    """Health check endpoint"""
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=12723)
