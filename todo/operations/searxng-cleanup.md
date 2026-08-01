# Clean up the unmanaged searxng-docker stack

The stack at `/opt/src/searxng-docker` was upstream's default compose
(searxng + redis + **caddy with host networking**,
`restart=unless-stopped`) and was NOT repo-managed; its caddy raced
nginx for :80/:443 and caused a tunnel outage (incident:
`../ycluster-private/incidents/2026-06-11 — searxng/caddy bind-race outage`).

## Done

- ✅ Stack removed 2026-08-01: containers stopped + `docker rm`'d on s1
  (searxng, caddy); `/opt/src/searxng-docker/` deleted. Open-WebUI's
  web-search path uses Kagi (see `app/build-open-webui.yml`), so no
  upstream consumer pointed at the local instance.

## Remaining

### Monitoring gap

The failed-systemd-units alert only sees systemd units — a restart-looping
docker container is invisible. Add container-state alerting (periodic
`Restarting`/RestartCount check feeding the existing alert path, or
cadvisor). Lower urgency now that the only docker containers left on
storage are intended (see `move-docker-to-podman.md`).

### Minor certbot warts

`obtain` writes the new cert but doesn't reload nginx (manual reload
needed on the obtain node), and `--test` deploys the *staging* cert to the
live endpoint — fix or document both in `certbot_manager.py`.
