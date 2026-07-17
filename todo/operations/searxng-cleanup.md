# Clean up the unmanaged searxng-docker stack

The stack at `/opt/src/searxng-docker` is upstream's default compose
(searxng + redis + **caddy with host networking**,
`restart=unless-stopped`) and is NOT repo-managed; its caddy raced nginx
for :80/:443 and caused a tunnel outage (incident:
`../ycluster-private/incidents/2026-06-11 — searxng/caddy bind-race outage`).

## Remaining

### Remove the stack entirely

Confirmed an unused experiment: `docker compose down` in
`/opt/src/searxng-docker`, then delete the dir; first check Open-WebUI
has no web-search config pointing at it. Until then only the stopped
container's `restart=no` prevents a `compose up` from resurrecting caddy.

### Monitoring gap

The failed-systemd-units alert only sees systemd units — a restart-looping
docker container is invisible. Add container-state alerting (periodic
`Restarting`/RestartCount check feeding the existing alert path, or
cadvisor).

### Minor certbot warts

`obtain` writes the new cert but doesn't reload nginx (manual reload
needed on the obtain node), and `--test` deploys the *staging* cert to the
live endpoint — fix or document both in `certbot_manager.py`.
