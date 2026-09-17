# Rootless inference containers need a live user session for podman memory limits

All GPU inference backends (`ds4`/deepseek-v4-flash, nemotron-3-nano-omni,
glm-5.3-flash) run as rootless podman under the `dev` user, launched by
`~/bin/*-start.sh` idempotent scripts. Rootless podman only enforces a
`--memory` cgroup cap when it has a systemd user session (systemd cgroup
manager) available at container-create time — rootless cgroupfs cannot
enforce limits.

In normal ops the scripts are run from an interactive `dev` login, which
provides a transient user session, so this works without lingering; the
running container keeps its delegated cgroup after the session ends. But the
assumption breaks in two automated paths:

- **Root-initiated starts** (cluster-exec, ansible via `sudo -u dev`): there
  is no `dev` user session, podman falls back to cgroupfs, and container
  create fails with an OCI `permission denied` under the root user slice
  (`user-0.slice`).
- **Boot-time / after-crash restarts**: with no interactive login and no
  lingering, there is no session and nothing brings the backend back up
  without an operator logging in as `dev`.

Compounding: a host-level OOM can SIGKILL the `dev` systemd user session
(`user@.service`) as an ordinary process, destroying the cgroup manager while
the rootless container keeps reporting "running" as a zombie — no listener on
the model port, no servable process.

## Proposed

1. Enable lingering for `dev` declaratively (`loginctl enable-linger`) via
   ansible/systemd so the user session exists at boot and under automation.
   (Currently it is not enabled; it was never needed because starts always
   came from an interactive `dev` login.)
2. Give each rootless inference backend a boot-time start path — a
   `dev` systemd user unit / quadlet, or a root unit whose `ExecStartPre`
   starts the user session and runs the existing `~/bin/*-start.sh` — so a
   reboot or a crash-then-restart no longer requires an interactive login.
3. Re-baseline the memory-cap story where a container was previously launched
   without an enforced cap (i.e. when the session was absent at create time).

## Verify before rollout

- After `loginctl enable-linger` and `user@.service` active, a root-initiated
  (out-of-band) launch applies the `--memory` cap and the backend serves.
- A host reboot brings each backend back without an interactive `dev` login.

## See also

- `docs/inference/glm-5.3-flash-deploy-2026-08.md` (co-residency and memory-cap
  notes for the GLM slice).
- `docs/operations/cluster-exec.md` (the `sudo -u dev` quirk on GPU hosts).
