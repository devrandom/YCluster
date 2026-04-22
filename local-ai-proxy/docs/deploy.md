# Deployment

## Systemd

Runs as an unprivileged user, one process. Example unit:

```ini
[Unit]
Description=local-ai-proxy
After=network.target

[Service]
Type=exec
ExecStart=/usr/local/bin/local-ai-proxy --config /etc/local-ai-proxy/config.yaml
User=local-ai-proxy
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
```

The proxy responds to `SIGTERM` with a 30-second graceful drain,
then force-closes any remaining connections.

## Nginx with auth_request

The proxy does not validate bearer tokens itself. Put nginx in front
with an `auth_request` that validates however you want (bearer table,
OIDC introspect, database lookup, etc.) and sets `X-User-Id` on the
upstream response. Nginx forwards that value to the proxy, which
logs it per request and trusts it only when the request came from a
CIDR in `trusted_proxies` (loopback by default).

```nginx
server {
    listen 80;
    server_name inference.example.com;
    client_max_body_size 50M;

    location = /__auth {
        internal;
        proxy_pass http://127.0.0.1:4002/auth;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header Authorization $http_authorization;
    }

    location / {
        auth_request /__auth;
        auth_request_set $user_id $upstream_http_x_user_id;

        # local-ai-proxy trusts X-User-Id from us and doesn't need
        # the bearer token.
        proxy_set_header Authorization "";
        proxy_set_header X-User-Id $user_id;

        proxy_pass http://127.0.0.1:4000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        # Streaming
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;

        # Long timeouts for inference
        proxy_connect_timeout 10m;
        proxy_send_timeout 10m;
        proxy_read_timeout 10m;
    }
}
```

The `/__auth` endpoint is yours to implement. It receives the
client's `Authorization` header and must respond with either:

- **200** and an `X-User-Id` response header → nginx allows the request
- **401** → nginx returns 401 to the client

A minimal one might look up a PostgreSQL api_key table or call an
internal OIDC introspection endpoint.

### Trusted proxies

By default the proxy only trusts `X-User-Id` from `127.0.0.1/32` and
`::1/128`. If nginx runs on a different host, extend the list:

```yaml
trusted_proxies:
  - 127.0.0.1/32
  - 10.0.0.0/24
```

Anything outside the trusted set has its `X-User-Id` stripped before
logging or forwarding, so an anonymous client connecting directly to
the proxy port can't forge an identity.

## Disabling a backend at runtime

With an etcd source, write an empty or JSON value under a
"disabled" prefix (default `/cluster/config/inference/disabled/`,
configurable via `etcd.disabled_prefix`):

```bash
etcdctl put /cluster/config/inference/disabled/http://gpu2:8000 '{"reason":"down for RMA"}'
```

Within one health-check cycle (default 30s) the proxy:

- stops polling that URL
- logs the state transition once at INFO (not WARN)
- reports `state: disabled` at `/healthz`
- hides models whose every backend is disabled from `/v1/models`

Remove the key to re-enable:

```bash
etcdctl del /cluster/config/inference/disabled/http://gpu2:8000
```

## Operational endpoints

### `/healthz`

```json
{
  "status": "degraded",
  "healthy": 5,
  "down": 1,
  "disabled": 1,
  "backends": [
    {"url": "http://gpu1:8000", "state": "healthy", "last_check": "2026-04-22T11:56:28Z"},
    {"url": "http://gpu2:8000", "state": "disabled", "last_check": "2026-04-22T11:56:28Z"},
    {"url": "http://mac1:8080", "state": "down", "last_check": "2026-04-22T11:56:28Z",
     "err": "dial: connection refused"}
  ],
  "models": [
    {"name": "llama-3.1-70b", "state": "healthy", "backends": ["http://gpu1:8000"]},
    {"name": "archived-model", "state": "disabled", "backends": ["http://gpu2:8000"]}
  ]
}
```

Overall `status` is a model-level rollup:

- `ok`: every model has at least one healthy backend
- `degraded`: at least one model is unavailable but others are healthy
- `down`: no model has a healthy backend
- `unknown`: nothing is checked yet

Disabled backends and implicitly-disabled models (every backend
disabled) do not contribute to `degraded` or `down`. HTTP status is
always 200 when the proxy itself is up — callers interpret the
`status` field.

### Logs

Structured JSON via `slog`. One line per request:

```json
{"time":"...","level":"INFO","msg":"request","method":"POST",
 "path":"/v1/chat/completions","status":200,"duration_ms":184,
 "bytes_out":5231,"user":"alice@example.com","remote":"127.0.0.1:55123"}
```

Health state transitions log at INFO (healthy, disabled) or WARN
(down), with the backend URL and error message.
