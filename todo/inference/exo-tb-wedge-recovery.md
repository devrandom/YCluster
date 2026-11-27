# exo: recover from a Thunderbolt wedge without a human

The TB link between the exo macs wedges every few months (cable untouched,
`system_profiler SPThunderboltDataType` → `No device connected` on every
receptacle of *both* macs, `en5` inactive on both ends, LAN fine). While it
is wedged, exo has no RDMA edge, so `place_instance` with `MlxJaccl` fails
with `Requested RDMA (MlxJaccl) but no RDMA-connected cycles available`
and there is no placement at all. Only a reboot of one mac clears it.

Today the only actor is `com.ycluster.exo-place` (`exo-place.sh`,
`SUPERVISE=1`), and it cannot help:

- it retries every 30 s forever;
- `POST /place_instance` answers 200 `{"message":"Command received."}` even
  when the master rejects the placement asynchronously, so the supervisor's
  log looks healthy;
- its liveness test is `instance_count > 0`, and a rejected placement never
  creates an instance, so "no instance yet" and "cannot place" are
  indistinguishable;
- it never falls back to a topology that does not need RDMA.

Fix shape:

1. **Watchdog** (`macos/setup-exo.yml`, new LaunchDaemon): ping the peer's
   TB address (10.0.2.1/10.0.2.2). After N consecutive minutes of failure,
   if the local TB port is also `status: active` but dead, `shutdown -r now`
   with a persistent cooldown marker so an unplugged cable cannot reboot-loop
   the pair. A one-end reboot is enough in practice.
2. **Supervisor** (`files/exo/exo-place.sh`): after `place`, poll
   `/state.instances`; if the instance has not appeared in ~60 s, log a
   distinct error and count it. After N failures, re-place with
   `INSTANCE_META=MlxRing` (TCP ring over the cluster LAN — slower TP
   all-reduce, but serving), and `purge` the local node first to clear
   inactive-page residue (`No cycles found with sufficient memory`).
3. **Log size guard** (`macos/setup-exo.yml`): `exo.log` is unbounded and a
   single catalog card with a nonexistent upstream repo produces a
   rich-variable 404 traceback every ~63 s even under `EXO_OFFLINE=1`.
   Card `mlx-community/GLM-4.7-8bit-gs32` is dropped by carried patch
   `patches/0002-drop-nonexistent-glm-catalog-card.patch`; add a
   truncate-when-above-N-MB guard anyway.
4. **Alerting** (`config/ansible/monitoring/templates/ycluster-alerts-inference.yml.j2`):
   `InferenceBackendDown`/`InferenceModelDown` are `warning` with a 2 h
   repeat; a single-backend model like K2.6 can sit down for a day without
   anyone acting. Decide whether a model with zero healthy backends should
   page (`InferenceModelDown` → critical) rather than warn.
5. **Upstream**: v1.0.71 is the current release. File that `EXO_OFFLINE`
   does not gate the download-status poll, that node ids are still not
   persisted, and that the event log is never compacted (a follower replays
   it at ~800 events/s, so a restart after weeks of uptime costs hours
   unless `~/.exo/event_log` is archived first — see `docs/operations/exo.md`).

See `docs/operations/exo.md` → "Thunderbolt controller can wedge" for the
diagnostic signals (`topology.connections` RDMA edge, not
`thunderboltBridgeCycles`).
