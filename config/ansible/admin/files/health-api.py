"""Health API — lite service for non-storage nodes.

Serves /api/health + /metrics on :12724 on every managed node. The service
checks live in health_common, health_checks, and health_report so this entry
point stays focused on the HTTP surface.
"""

from flask import Flask, jsonify

from health_common import _HOSTNAME, check_vip_status, init_request_cache
from health_report import get_comprehensive_health, service_metric_value

HEALTH_API_PORT = 12724

app = Flask(__name__)
app.before_request(init_request_cache)


# ---- routes -------------------------------------------------------------

@app.route('/api/health')
def health():
    health_status = get_comprehensive_health()
    status_code = 200 if health_status['overall'] == 'healthy' else 503
    return jsonify(health_status), status_code


@app.route('/metrics')
def prometheus_metrics():
    try:
        health_data = get_comprehensive_health()
        metrics = []

        overall_value = 1 if health_data['overall'] == 'healthy' else 0
        metrics.append(f'ycluster_node_healthy{{node="{_HOSTNAME}"}} {overall_value}')

        for service, details in health_data.get('services', {}).items():
            service_value = service_metric_value(details.get('status'))
            metrics.append(
                f'ycluster_service_health{{node="{_HOSTNAME}",'
                f'service="{service}"}} {service_value}'
            )

        # vip metrics
        vip_status = check_vip_status()
        gateway_vip_active = 1 if vip_status['gateway_vip']['active'] else 0
        storage_vip_active = 1 if vip_status['storage_vip']['active'] else 0
        metrics.append(
            f'ycluster_vip_active{{node="{_HOSTNAME}",'
            f'vip="gateway"}} {gateway_vip_active}'
        )
        metrics.append(
            f'ycluster_vip_active{{node="{_HOSTNAME}",'
            f'vip="storage"}} {storage_vip_active}'
        )

        response = "\n".join(metrics) + "\n"
        return response, 200, {
            "Content-Type": "text/plain; version=0.0.4; charset=utf-8"
        }
    except Exception as e:
        error = f'ycluster_metrics_error{{node="{_HOSTNAME}"}} 1\n'
        return error, 500, {
            "Content-Type": "text/plain; version=0.0.4; charset=utf-8"
        }


def serve_production():
    """Run waitress: production WSGI with a bounded thread pool.

    This service is scraped cross-node by cluster-heartbeat, so don't use
    Flask's development server here.
    """
    from waitress import serve
    serve(app, host='0.0.0.0', port=HEALTH_API_PORT, threads=8)


if __name__ == '__main__':
    serve_production()
