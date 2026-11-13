# Intrusion detection system

No IDS today: we collect logs and metrics, but nothing detects or alerts on
hostile behaviour. Authentik gates public HTTP paths and SSH is key-only, yet a
non-root compromise, a stolen non-root key, or a reachable service that should
not be would only surface if a human read the logs. This item scopes an IDS
that fits the existing stack (journald, Prometheus/Alertmanager, ntfy) instead
of adding a separate SIEM control plane.

Design: [`docs/design/intrusion-detection.md`](../../docs/design/intrusion-detection.md).

## Alerting target

The cluster averages **one forwarded IDS alert per day**. Sustained volume
above that is a tuning defect, not vigilance: each alert either points at a
routine shape to classify at the source, or at a real policy question to
decide and record. Deploy and operator activity that a person deliberately
started does not belong in the average — if it can be mistaken for a
detection, it is a missing classification, not an alert.

The notification path also enforces a storm brake: at most **five alerts per
severity per hour for the whole cluster**. Each severity has an independent
budget, so a warning storm cannot consume the capacity reserved for criticals.
The budget is enforced by `ntfy-webhook`, the shared notification edge, with
atomic slot claims in etcd; the forwarder is per-host, so a local cap there
would multiply the budget by the fleet size. The forwarder keeps every
classified detection in `detections.jsonl` regardless of the budget. The
`-rate-limit` flag remains an optional per-host circuit breaker, disabled in
the Ansible deployment. The Incus audit rule (`container-api`) is excluded
from the budget — it is already coalesced to one alert per hour and must not
consume a detection's slot. The first over-budget alert for a severity sends
one additional notice explaining that the cap was reached; later over-budget
alerts are recorded but not delivered until the next window. During the
tuning window `IDSAlert` still routes to `ids-silent`, so this budget governs
the path that will carry pages once that route is removed.

## Threat model

Two classes of adversary are in scope:

- **External attackers.** Someone outside the cluster probing or exploiting the
  public ingress, WireGuard endpoint, or any service that is unexpectedly
  reachable, before they obtain host root. Assume perimeter controls can be
  misconfigured, not that they hold.
- **Malicious users.** VM owners escaping or attacking neighbours, non-root
  users abusing legitimate access, a stolen non-root SSH or WireGuard key, or a
  non-root insider running commands they should not. Detection has to cover
  actions that look authenticated and authorised at the edge, so perimeter logs
  alone are insufficient. Privilege escalation from a non-root identity is in
  scope up to the point root is obtained.
- **Host root is out of scope for host-based detection.** An attacker who
  already has root on a monitored host disables or tampers with the sensor
  before acting. Root interactive sessions, root SSH commands, a stolen root
  key, and post-root activity are evidence-only; they are not coverage claims.

## Scope

- **Host-based (HIDS).** Process/exec, file-integrity and login monitoring on
  every node, Incus host, and remote host joined to the cluster. Cover:
  unexpected setuid/capability changes, writes to `/etc` and systemd unit paths,
  new listening sockets, shells spawned by service users, and sudo/sshd
  authentication anomalies. Per-host sensors are required, not optional (see
  below). Admin laptops are out of scope for now.
- **Network-based (NIDS).** Flow/alert coverage for ingress and east-west
  traffic. Minimum: public ingress (frp/rathole front), the WireGuard
  endpoint, and storage/compute inter-node traffic. Use an in-cluster sensor
  on the ingress host rather than assuming a managed switch can mirror. This
  complements the per-host sensors; it does not replace them.
- **Container/VM layer.** Audit Incus lifecycle events and container exec
  activity, so a workload escape or an unexpected `incus exec` is visible.
  Host-side sensors see VM traffic and escapes but not activity inside a guest
  OS; unless an agent is installed in guests, malicious-tenant detection stops
  at the hypervisor boundary. Decide explicitly whether that is acceptable.

## Per-host sensors

Every node and Incus host runs its own sensor. This is not just a core-node
deployment: remote cluster nodes matter because they sit outside the LAN, may be
powered off or disconnected for long periods, and cannot be covered by the
in-cluster network sensor. A remote node can still be the entry point for a
stolen non-root credential or a compromised non-root operator session. Admin
laptops are out of scope for now and should not gate the rollout.

- **Deploy with the host baseline.** Use one Ansible role for Linux hosts so the
  sensor is installed, configured, and re-applied like any other baseline
  service. Do not block this work on a macOS sensor; revisit admin laptops as a
  separate item.
- **Push, don't pull.** Ship events to the central collector over the
  WireGuard/tunnel path using an authenticated, encrypted transport. Do not
  depend on inbound reachability to a remote host, and do not require a
  long-lived central connection to be up before local detection works.
- **Buffer locally and replay.** When the collector is unreachable, buffer on
  the host with a bounded disk budget, replay in order, and mark the gap
  explicitly. Remote hosts will be offline; the plan must not lose events or
  grow the buffer without limit.
- **Treat coverage loss as an alert, not as a defence.** A host that stops
  reporting, whose sensor is disabled, or whose buffer overflows is itself an
  alert. This catches accidents and careless attackers. It does not stop a root
  attacker: they disable the sensor first, take the coverage alert, and are
  still out of scope by the threat model. Say so rather than implying the alert
  closes the case.
- **Protect the local state.** Assume the host can be compromised: keep the
  sensor's buffer and credentials out of the root fs, use a fixed budget, and
  document what a compromised host can read or tamper with. Prefer
  append-only/forward-only shipping over trusting a local database.
- **Keep clock discipline.** Remote hosts may drift or hibernate; timestamp
  events in UTC, record clock source/offset, and do not rely on local time
  alone for correlation.

## IP retention

Hard constraint: the IDS must not retain user IP addresses (client addresses and
VM-tenant addresses) longer than the existing nginx/SSH access-log retention. It
must not become a second, longer-lived access log.

- **Same window, same deletion.** IP-bearing IDS records use the access-log
  retention window and are purged on the same schedule. Do not configure a
  separate, longer retention for flow records, alerts, or captures.
- **Real-time use is fine; persistence is the constraint.** Detection and
  correlation may use IPs live in memory. Anything written to disk must either
  expire with the access logs or store no IP.
- **Long-term records are IP-free.** Aggregate by host/VM identity, event type
  and count. Do not put user IPs in Prometheus labels (central retention is
  90d), Alertmanager annotations, ntfy messages, or incident notes.
- **Buffers and replay.** Per-host buffers drop IP-bearing events older than the
  access-log cutoff; replay must not reintroduce expired addresses. Size the
  buffer by time as well as bytes.
- **Packet capture.** Raw pcap contains IPs by definition. Keep captures only
  inside the access-log window and delete them on the same schedule. Once the
  window closes, incident response works from non-IP evidence or anonymised
  summaries; a capture cannot be held open "for the investigation".
- **Enforce it.** Add a scheduled purge plus a check that scans IDS stores for
  IP-bearing records older than the access-log retention, so a config drift fails
  visibly instead of silently extending retention.

## Work

- Implement the design's Go-first stack: Tetragon per host, CrowdSec
  local-only for log behaviour, and the in-repo Go `ids-forwarder`. Build from
  source at pinned versions; no prebuilt binaries. Wazuh and the other C/C++
  parsers are rejected as core sensors in the design.
  - Done: `ids-forwarder` (`ids-forwarder/`) with Tetragon JSON tailer,
    optional CrowdSec HTTP receiver, IP redaction, dedup, durable replay
    cursor, bounded fsynced spool with backpressure, Alertmanager v2
    posting, and localhost metrics; build and install playbooks
    (`app/build-ids-forwarder.yml`, `monitoring/install-ids-forwarder.yml`).
  - Done: retention-aware replay: restart and rotation recovery from
    current and gzip-rotated Tetragon files, an exact scan-order cursor
    boundary (no timestamp watermark), a distinct `IDSForwarderGap` alert
    when the boundary has aged out, and Prometheus backlog alerts for queue
    depth and spool bytes.
  - Done: Tetragon build (`app/build-tetragon.yml`, source at pinned SHA,
    eBPF compiled with LOCAL_CLANG) and observe-only deployment
    (`monitoring/install-tetragon.yml`): gRPC on unix socket only,
    loopback metrics, JSON event export for ids-forwarder, pinned
    policies for exec/sensitive-file/capability/kernel-module/connect.
  - Done: CrowdSec build (`app/build-crowdsec.yml`, v1.8.1 source) and
    local-only deployment (`monitoring/install-crowdsec.yml`): LAPI on
    loopback, no console/CAPI/community blocklist, no bouncers, pinned
    scenarios (ssh-bf, http-crawl, http-40x-burst) and parsers, alerts
    delivered to ids-forwarder's localhost receiver via notification-http.
  - Done: first-pass volume tuning: targeted privilege-gain and actual
    module-load policies, corrected TCP-connect matching, expected private
    control-plane TCP pairs classified at the policy/forwarder boundary,
    process-exit export removed, routine exec probes and the managed
    WireGuard reconcile write suppressed, sshd/cron root transitions
    filtered at the policy, systemd's transient fallback handled by the
    forwarder, and volatile identifiers normalized for deduplication.
  - Remaining: policy/scenario tuning window on the dev cluster, raising
    ids-forwarder module coverage to the 95% cap, Authentik log shipping
    into /var/log/crowdsec/ids-authentik.log.
  - Remaining: name every detection after the Falco or Sigma rule it ports,
    per "The behavioural-rule gap" in the design; do not add a rule without an
    upstream counterpart.
  - Done: `ids-forwarder` copies every `process_exec` and `tcp_connect` event
    to RSigma over a bounded unix-socket queue, bypassing the `execBinaries`
    allowlist for the Sigma path only. RSigma detections return through the
    forwarder's normal Alertmanager spool. The hand-written Go rules still see
    only the allowlist; decide whether to widen it before retiring the
    duplicate coverage.
  - Done: the Sigma rule set now includes SigmaHQ's execution-from-`/tmp`
    and execution-from-`/dev/shm` rules. A replay over four nodes' Tetragon
    logs (~1.1M execs) showed no routine hits, so the rules add no volume.
    A file-ownership rule was rejected: the forwarder cannot read home
    directories and legitimate operator virtualenvs make it noisy. Added the
    local `exec-root-user-path` rule instead (root executing from `/home` or
    `/var/tmp`, host layer only), also zero hits in the replay. A non-root
    operator running their own binary stays indistinguishable from an
    attacker by exec metadata alone.
  - Running: Sigma/RSigma pilot on the installed service, using the curated
    rules under `config/ansible/monitoring/files/rsigma/rules/`. Started
    2026-11-04 (first detections), checkpoint 2026-11-18. Day-0 tuning added
    local suppressions for the MicroCeph daemons (887 detections in three
    days, all of the pilot's noise) and for sysstat/dmesg rotation, whose
    upstream filter assumes a bare `rm -f ...` command line. Pass criteria and
    the readout are in "The behavioural-rule gap" in the design doc. Keep the
    hand-written rules until it passes.
  - Done: tenant-container egress is classified by layer. Internet egress from
    an Incus container is suppressed as routine tenant traffic, while a
    container connecting to a cluster-private address still alerts: such a
    connection should be impossible, so the alert fires when network policy is
    broken or bypassed. Container identity comes from `/proc/<pid>/cgroup`
    with a PID/start-time check, recorded as a `container` label.
  - Done: host egress is classified by destination, not origin. A TCP connect
    to a public destination, and a plain `curl`/`wget` public fetch, are
    routine for every process and are not alerted, including a shell whose
    entire payload is one such fetch. A private cluster destination still
    alerts as lateral movement; config-file, proxy, unix-socket, composed and
    piped commands stay visible.
  - Done: reboots do not page. For five minutes after boot, routine unit and
    unknown-origin activity (Incus API readiness, container hooks, VM
    launches, dnsmasq, mounts, nftables module loads and shell wrappers) is
    recorded but not delivered. Interactive sessions, private lateral
    connects, file writes, Sigma rules and `forwarder-gap` remain visible.
  - Open: re-examine the boot sequence. The five-minute grace leaves a blind
    spot for a compromised service or malicious systemd unit active during
    boot: its activity is recorded but does not alert. Replace the broad
    grace with unit- and parent-anchored classification of the actual boot
    sequence, add a positive control proving a hostile unit still alerts, and
    decide whether one "boot activity" summary should be delivered. Build the
    boot unit/shape inventory from a clean reboot.
  - Open: align delivery with the host-root-out-of-scope model. Root interactive
    `process_exec`, root `privilege-raises`, and root-origin private
    `tcp-connect` are still delivered. Move them to `detections.jsonl`-only,
    or record why they remain as evidence. Guest root is unaffected by this:
    uid 0 inside a tenant container or VM is a host-unprivileged identity and
    must keep alerting, so any filter has to key on `Layer: host` plus a host
    login session or host unit, never on uid alone. Non-root sudo and
    escalation alerts stay regardless. File writes below `/etc` are root by
    construction, so they follow the same rule and were never in the paging
    subset. The classification, the `page` label and the measurement gate are
    specified in
    [`docs/design/ids-coverage-and-evidence.md`](../../docs/design/ids-coverage-and-evidence.md).
    Deployed and verified: root execs are recorded with
    `class=root_exec delivery=evidence` and never reach Alertmanager, a live
    non-root exec lands in `exec_nonroot` with `page=no` and increments
    `ids_forwarder_page_candidates_total`, and the shadow counters are in the
    node textfile collector. Two gaps are left in the measurement itself:
    a short-lived non-root host process whose pid has left `/proc` falls back
    to `layer=unverified` and is counted as `container_other` rather than
    `exec_nonroot`, so the class undercounts on replay and for very short
    commands; and `escalation_nonroot` pages from day one without a shadow
    measurement, by decision.
  - Done: Incus API usage is recorded. `incus` is on the forwarder's exec
    allowlist and maps to its own rule, `container-api`, so every `incus
    exec`, `incus file push/pull` and lifecycle call lands in
    `detections.jsonl` with the subcommand and target instance. Delivery is
    one alert per host per hour regardless of subcommand; the record keeps
    every call that is not classified. Known periodic consumers are
    classified instead of recorded: `gpu_collector` for
    `vm-gpu-collector`'s list and guest `nvidia-smi` queries, and
    `nat_sync` for the frontend-NAT reads of instance metadata and the
    bastion's `/etc/hosts`. This is the audit trail for entering a container or VM
    namespace. The daemon's own debug log is deliberately not used for this:
    it dumps whole API response bodies (~12-40 MiB/hour on one host) and
    still reports the unix-socket peer as `@`.
  - VM lateral movement. Host-side Tetragon cannot see guest sockets — the
    hypervisor only exposes the QEMU process — so a VM reaching a cluster
    address produces no kprobe event. Deployed on every bridge host: the
    bridge guard — a traffic filter, not an IDS component,
    `admin/install-vm-bridge-guard.yml` — drops guest-to-neighbour traffic in
    the nftables bridge family and counts/logs guest-to-cluster attempts ahead
    of the silent Incus ACL verdict. The IDS side is separate
    (`monitoring/install-ids-bridge-consumer.yml`) and posts one CrowdSec-shaped
    alert per class per hour to the forwarder's loopback receiver — no guest
    address leaves the host. Two survey findings changed
    the design: the Incus ACL is emitted with drops before allows, and it is
    never consulted for guest-to-guest traffic at all, because same-bridge
    frames are switched at layer 2 and skip the IP hooks. Remaining: flow
    records (a scan and a single connection still look the same) and watching
    the neighbour-drop counter, which is the only signal that a real
    tenant-to-tenant dependency got cut. Details in the design doc's *VM bridge
    visibility* section.
- Define the detection rule set and tune it to this cluster's baseline before
  enabling alerts; default signatures will be noisy against the inference and
  container workloads.
- Evasion-analysis follow-up: Sigma delivery fixed (the unit's
  space-separated bool flag stopped Go flag parsing, leaving the socket,
  cursor, spool and receiver unparsed) and `IdsSigmaStalled` added; inert
  hostname rules disabled; attacker-shaped egress suppressions partly
  tightened; nginx/sshd parser defects fixed. Paging decided on 2026-11-08:
  `exec-root-user-path`, `kernel-modules`, `privilege-raises` and Sigma at
  medium or above page; `container-api` and the bridge-guard blocks stay
  observe-only. The routine shapes behind `privilege-raises` (session spawns,
  unattended-upgrades, squid rotation) are classified first, so it is not a
  proxy for timers. Still deferred to the discussion phase:
  VM/UDP/unix-socket visibility, rename/mmap/read hooks,
  the admin-service port range, alert-path
  authentication, CrowdSec scenarios/thresholds, non-root interpreter
  coverage and raw retention. Boot-order attribution is fixed in the
  forwarder: an event that Tetragon reports as `/usr/lib/systemd/systemd` with
  argv `text` now has its identity resolved from `/proc`, with the pid's file
  start time checked against the event before the lookup. Events replayed
  after the process has exited keep the placeholder.
  The concrete actions and decision points for those items are in the design
  doc's *Next steps* section.
- Ship findings through the existing Alertmanager → ntfy path, with a
  severity mapping. Avoid a second paging channel that bypasses the
  established alert routing.
- Define purge/retention for the sensor's own data, capped by the existing
  access-log retention (see IP retention above), and where it lives (not on the
  storage root fs — see the MON_DISK_LOW incidents).
- Add a playbook/timer for rule updates and a documented procedure for
  triage and containment (isolate node, revoke WireGuard peer, rotate keys).
- Add the per-host sensor to the host baseline and inventory-driven rollout,
  including remote hosts, and define how a host that is down, off-cluster, or
  outside the Ansible inventory is represented in coverage.

## Suppression blind-spot review

Review assumption: the attacker knows the policies, forwarder source, Sigma
rules and CrowdSec content. A suppression is unsafe when it matches a value
the attacker can set (argv, basename, cgroup text, mutable container path) or
when a later stage removes the event that would have shown the bypass.

Actionable in this pass:

- [x] Container attribution must not accept a forged cgroup name or a host
      subuid: parse only real LXC/Docker/Podman/Kubernetes roots, assign user
      scopes an `unverified` layer, and raise the no-`/proc` idmap fallback to
      the Incus base.
- [x] Egress/lateral exemptions must use exact executable paths (or anchored
      snap paths), not `path.Base` values that any user can copy to `/tmp`.
- [x] Interpreter exemptions must use the actual script operand, resolve
      `..` traversal, and match exact destinations; `adminPythonDial` and
      the blackbox updater are the first cases. Symlinks are not followed:
      the remaining paths are root-owned install locations.
- [x] Shell and Ansible suppressions must match anchored command shapes, not
      substrings that can appear in a comment or a later argument; a probe
      command must not be able to carry a payload.
- [x] The Ansible temp-directory and node-exporter sponge shapes must reject
      shell metacharacters in their variable tokens.
- [x] Health probes must use known system binaries, reject proxy/resolve/
      unix-socket redirects, and require every HTTP URL to be loopback.
- [x] Health probes must reject shell composition and config indirection
      (`-K`, `-e`, `-o`, `-O`), and address/socket tests must parse the host
      rather than use substrings.
- [x] Package queries and maintenance must match the complete command shape and
      the apt subcommand position, not a prefix or any later token.
- [x] Reject apt `Pre-Invoke`/`Post-Invoke` and arbitrary `-c` config files;
      require the exact verifyrestore writer and `<version>/verifyrestore`
      components.
- [x] The bridge-guard module exemption must require the package `modprobe`
      and the exact module name, not any command line containing the token.
- [x] File-write suppressions must use exact writer paths and the narrow target
      set each writer manages.
- [x] Dedup keys must include severity and the unredacted Sigma destination,
      so a critical event cannot disappear behind an earlier warning and two
      callback destinations cannot collapse into one.
- [x] Sigma local suppressions must require `Layer: host`; the normalizer must
      carry layer and container identity so a tenant cannot match a host
      filter.
- [x] `container-api` and `container-exec` must match the real Incus binaries,
      not any executable whose basename is `incus` or `incusd`.
- [x] The Tetragon measurement switch must remove the current `1s` limit and
      its `rateLimitScope`, not the retired `1m` value.
- [x] Alertmanager must group and inhibit on `host` and `rule_id`, not on an
      absent `node` label; missing equal labels currently match across hosts.
- [x] CrowdSec must classify SSH success/failure from the message prefix and
      nginx static resources from the cleaned path, not a substring or the
      query string.
- [x] Nginx static resources must use known static roots, not an arbitrary
      file extension, and a `..` segment must not inherit a static root.
- [x] Sigma must apply the same Incus idmap fallback when the process is gone,
      so a dead container root shell cannot match `Layer: host`.

Second delta, after re-running the deployed review:

- [x] Health probes reject `$()` and attached/equals forms of config, proxy,
      output, execute and socket flags; `-o /dev/null` remains allowed.
- [x] Apt attached `-c`/`--config-file` forms are rejected, not only the
      separated form.
- [x] Verifyrestore requires `/bin/install` or the exact
      `pg_createcluster <version> verifyrestore` shape and a
      `postgresql/<version>/verifyrestore/` path.
- [x] The `unverified` layer does not suppress loopback, bridge or private
      destinations; only ordinary public egress is routine.
- [x] A dead non-root process without a cgroup is `unverified` rather than
      host, and Sigma checks the parent start time before falling back.
- [x] Nginx static classification rejects encoded `..` (`%2e`/`%2E`).

Third delta, after deploying the second delta and re-running the review with
the working tree included:

- [x] A process that `/proc` still resolves keeps the host layer even when it
      runs as a non-root service account. The old code applied the uid
      fallback to resolved host processes, so every non-root daemon
      (`admin-api`, `local-ai-proxy`, `prometheus`, the `ceph` CLI) became
      `unverified`, lost `expected_tcp`, and alerted on every cluster-internal
      connect: about 1,000 `tcp-connect` alerts per hour on one node with the
      kernel `1s` limit still in place. The uid fallback now runs only when
      neither the process nor its parent resolves, in both the Tetragon and
      Sigma paths.
- [x] Ansible's nested become probe shapes are suppressed again. A non-root
      connection wraps the command twice, so the deployed argv is
      `bash -c "/bin/sh -c 'sudo -H -S -n -u root /bin/sh -c ...'"`; the
      forwarder now unwraps one shell layer at a time and matches the same
      anchored payloads. The expanded temp-directory form with backquote
      `echo`, the `rm -f -r .../ansible-tmp-*/` cleanup, the `env -i
      run-parts` motd shape and the `python3.x && sleep 0` interpreter probe
      are covered, as is the `chmod u+x .../ansible-tmp-*/ {dir}/source`
      step; a payload appended to any layer keeps the event visible.
      Without this, one deploy produced about 1,500 `process_exec` detections.
- [x] Clustered short options no longer bypass the health-probe checks:
      `curl -so /tmp/x`, `curl -sK /tmp/rc` and `wget -qO /tmp/x` are not
      routine, while `-o /dev/null` and `-m 5` still are.
- [x] A newline in a shell payload is rejected before whitespace
      normalisation, so `bash -c $'curl http://localhost/\\n/opt/evil'` cannot
      read as a single probe command.
- [x] The verifyrestore drill accepts `/usr/bin/install`, which is the binary
      Debian's `install` resolves to on these hosts, as well as
      `/bin/install`.
- [x] The PCP cleanup filter requires a file component that does not start
      with a dot, so `.../pmlogger/../../../../var/log/auth.log` cannot borrow
      the rotation exemption.
- [x] The node-exporter sponge exemption is limited to
      `/var/lib/prometheus/node-exporter/*.prom`, and the microceph snap
      rotation exemption matches the exact `run --timer=... <app>` shape
      instead of any argument that contains the app name.
- [x] The root-session Sigma filter description now states what the filter
      actually matches: any root interactive bash on a host, which stays
      visible through the `process_exec` rule.
- [x] The curl/wget loopback exemption is anchored to its only host-side
      originator: keepalived's storage-VIP script
      (`/usr/local/lib/cluster/check-storage-vip-health`) running
      `curl -f -s http://localhost:<port>/v2/`. A direct curl or wget with a
      loopback URL is no longer routine, whichever arguments it carries, and
      the shell health-check class now also requires a health-check parent
      (shim, runc, tini or the lifecycle wrapper).
- [x] apt-daily's `touch /var/lib/apt/periodic/<stamp> 2>/dev/null || true`
      is classified as package maintenance again; the earlier narrowing to
      the `systemctl start --no-block apt-news.service` shape had dropped it.
- [x] A dead process whose uid is below 1000 stays host. The unverified
      fallback was also catching system accounts that exit before the line is
      read: `_apt` (42) running `dpkg --print-foreign-architectures` from the
      hourly apt refresh, and PCP's `pcp` (996) running its log cleanup. Each
      produced 25-100 `process_exec` or Sigma detections per node per day, and
      the `Layer: host` PCP filter could not match them. Rootless runtimes run
      as the invoking login user, so the fallback now applies only at uid
      1000 and above.
- [x] The PCP cleanup filter allows the per-host subdirectory PCP creates
      (`/var/log/pcp/pmlogger/<host>/<file>`) while still rejecting a `..`
      component; verified with `rsigma rule backtest` against the three real
      shapes plus a log-clearing true positive.

Deferred to the discussion phase, because each needs a policy or volume
decision:

- The 8000-9999 internal-service range stays broad by design; narrowing it to
  the service registry conflicts with the earlier decision not to edit the
  filter on every new backend.
- Kernel `rateLimit` still drops events before userspace; replacing it with a
  counted forwarder cap needs a measured volume budget.
- Rename/link/mmap hooks and a periodic integrity baseline need a coverage and
  volume decision. Until then the managed Prometheus temp-write suppression
  must stay, and the documentation must not claim the rename is visible.
- The Vikunja healthcheck exemption still matches a path inside the tenant
  container. Fixing it needs an image or container-name identity, or moving
  the probe to the host.
- Root execution outside `/home` and `/var/tmp` (for example `/opt`,
  `/usr/local`, `/var/lib`) needs an allowlist of system-owned trees; broad
  coverage would alert on routine cluster services.
- Shell coalescing remains one alert per host per window. Per-actor coalescing
  and paging unknown-origin shells are policy choices about alert volume.
- Paging the remaining `process_exec`, `tcp-connect`, CrowdSec and Sigma-low
  classes, authenticating the paging path, and changing CrowdSec thresholds
  are separate decisions.
- Bridge-guard attribution by guest-selected source address/MAC needs an
  interface-to-instance mapping from Incus; the current consumer can be
  spoofed into another guest's budget or quiet list.
- Rootless Podman/Docker workloads whose cgroup is under `user.slice` get the
  `unverified` layer: execs stay visible, host automation and `Layer: host`
  Sigma filters do not apply, and network traffic uses the container egress
  rules. Verify the runtime through an API or an authenticated cgroup marker
  before restoring container-internal suppression for these workloads.
- The exact Ansible `BECOME-SUCCESS`/sudo probe shapes still launch a Python
  interpreter whose stdin can carry code. Removing that exemption needs a
  dedicated automation identity (or a non-shell probe), otherwise every
  Ansible task alerts.
- Raw event retention and the 50 MB rotation cap remain a forensic-window
  decision.
- `process_exec` only reaches the forwarder for the configured binary list
  (shells, privilege tools, package managers, downloaders). A payload at
  `/opt/evil`, `python3` by itself, `nsenter`, `rm` or a copied binary
  produces no event. Widening the list needs a volume budget; until then the
  Sigma rules are the only coverage for those paths.
- Environment-driven configuration defeats the shell exemptions: `.curlrc`
  via `CURL_HOME`, `APT_CONFIG` with an apt `Pre-Invoke` hook, `WGETRC` and
  `LD_PRELOAD` all run code or add destinations while the argv still matches a
  routine shape. Closing this needs a trusted parent/unit or a dedicated
  automation identity, not another argv check.
- Destination exemptions still trust the executable name for daemons that read
  attacker-supplied configuration (`prometheus`, `frpc`, `squid`,
  `smokescreen`, `docker-proxy`, `rootlessport`), and any host process may
  reach port 3128 and be treated as the squid proxy. Match the unit identity
  and the configured destination instead.
- A container process that enters the host mount namespace (`nsenter -t 1 -m`)
  keeps its container cgroup, so its file writes are still
  `container_internal`. Compare `/proc/<pid>/ns/mnt` with the host before
  granting the container layer, and define the behaviour when the pid is gone.
- The verifyrestore write path is still a generic `install` into a
  `postgresql/<version>/verifyrestore/` tree. It needs the backup unit's
  identity (or a per-run component) rather than a path that any root process
  can reproduce.
- The root-session Sigma filter suppresses the Bash Interactive Shell rule for
  every root interactive shell on a host. The description now says so; whether
  the pilot keeps that shape (the `process_exec` rule still records it) or
  narrows it to a parent/command-line condition is a policy choice.

## Packet capture

Decide this separately for **detection** and for **incident response**; they have
different costs and privacy exposure.

- **Continuous capture is probably not affordable or necessary.** At ingress and
  east-west rates, full-payload pcap for every flow means terabytes per day and
  a retention problem on a cluster whose root disks are already tight. Start
  with flow records (NetFlow/IPFIX or Zeek connection logs) plus IDS alert
  metadata always on, and keep packet capture as a bounded ring buffer or an
  on-demand action.
- **On-demand capture is the pragmatic default for IR.** When an alert fires,
  trigger a time-boxed capture on the relevant interface (ring buffer sized in
  the low GB, e.g. 1–4 GiB, rotated) and attach it to the incident. This gives
  usable evidence without a permanent full-packet archive. Define who can start
  a capture, how long it runs before it stops itself, and that it is deleted at
  the access-log retention cutoff.
- **If continuous capture is required, sample or narrow the scope.** Capture only
  the public ingress interface, or only traffic to/from a suspect VM or peer,
  with an explicit packet-per-second or byte budget. Do not enable it on every
  bridge "just in case".
- **Placement.** The in-cluster sensor sits on the ingress host: capture the
  external-facing interface before NAT/termination, and the internal bridge or
  veth path for east-west and VM-to-VM traffic. Router-side mirroring is not
  assumed to be available. Note that the ingress host only sees traffic that
  actually traverses it; storage/compute-to-compute flows need a sensor on a
  core node or a tap on the relevant bridge.
- **TLS limits what pcap buys.** At the ingress, HTTPS is ciphertext: pcap
  supports flow, volume, timing and connection-metadata detection, not payload
  signatures. Plaintext upstream traffic (nginx → local app) may be visible on
  loopback or the bridge; decide whether to capture it, because that exposes
  authenticated sessions and user content.
- **Privacy and access.** Captures may contain user VM traffic and session
  metadata. Restrict who can retrieve them, keep them out of the repo and out of
  general log storage, and apply the IP-retention constraint above. Prefer
  metadata/flow records when they answer the question. This matters especially
  for the malicious-user threat model, where the person under investigation may
  have legitimate access to cluster tooling.
- **Storage and retention.** Put capture files on a dedicated volume with a
  fixed budget, not the storage root fs (see the `MON_DISK_LOW` incidents).
  Retention is the access-log window, not a separate policy; rotation and the
  scheduled purge must enforce it, and the sensor must not fill the volume even
  under a capture storm.
- **Correlation.** Timestamp captures and IDS alerts in UTC against journald
  and Incus events so an incident can be reconstructed across the ingress,
  host, and VM layers; record a hash of each exported capture for integrity.

## Open questions

- Provenance is now recorded per detection (`origin=interactive|unit|
  container`, `actor=<user|unit>`, counted in
  `ids_forwarder_detections_total`). The host-root-out-of-scope model changes
  the routing decision: a root interactive shell or Ansible task is evidence,
  not coverage, because an attacker with host root disables the sensor first.
  Dropping the page must not drop the `detections.jsonl` record, or a later
  incident loses the only trail of what the root session did. The remaining
  policy question is therefore the non-root boundary, not root: escalation from
  a non-root identity up to root, non-root SSH and WireGuard, tenant/container
  lateral movement, and non-root exec from attacker-writable paths stay as
  alerts. The candidate design is a per-actor budget with a session digest
  instead of per-event pages; separating the automation identity from the human
  one (playbooks as a dedicated user) is still what makes Ansible
  distinguishable, since every task is a fresh login session.
- Which malicious-user paths matter most in practice: guest-to-guest traffic,
  abuse of non-root operator credentials, or misuse of the Ansible/agent access
  path by a non-root identity?
- What non-IP evidence is sufficient for incidents that surface after the
  access-log retention window has closed, and who owns that data?
- Which remote cluster nodes can run a sensor at all, and what is the minimum
  acceptable coverage and buffer policy for nodes that are offline for long
  periods?
- Does the ingress host have enough headroom for a sensor, or should the sensor
  run on a dedicated core node and receive traffic via a tap?

Related: [`audit-log-story.md`](audit-log-story.md) (journald aggregation and
retention), [`etcd-rbac-by-cert.md`](etcd-rbac-by-cert.md) (blast radius of a
single host compromise).
