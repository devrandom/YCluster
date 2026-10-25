# Intrusion detection system

No IDS today: we collect logs and metrics, but nothing detects or alerts on
hostile behaviour. Authentik gates public HTTP paths and SSH is key-only, yet a
compromised node, a stolen key, or a reachable service that should not be would
only surface if a human read the logs. This item scopes an IDS that fits the
existing stack (journald, Prometheus/Alertmanager, ntfy) instead of adding a
separate SIEM control plane.

Design: [`docs/design/intrusion-detection.md`](../../docs/design/intrusion-detection.md).

## Threat model

Two classes of adversary, both in scope:

- **External attackers.** Someone outside the cluster probing or exploiting the
  public ingress, WireGuard endpoint, or any service that is unexpectedly
  reachable. Assume perimeter controls can be misconfigured, not that they hold.
- **Malicious users.** Anyone with legitimate access who abuses it: VM owners
  escaping or attacking neighbours, a compromised operator/agent credential, a
  stolen SSH or WireGuard key, or an insider running commands they should not.
  Detection has to cover actions that look authenticated and authorised at the
  edge, so perimeter logs alone are insufficient.

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
stolen credential or a compromised operator session. Admin laptops are out of
scope for now and should not gate the rollout.

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
- **Treat coverage loss as an alert.** A host that stops reporting, whose
  sensor is disabled, or whose buffer overflows is itself an alert. Otherwise
  an attacker can disable the sensor and disappear from the console.
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
    optional CrowdSec HTTP receiver, IP redaction, dedup, bounded queue,
    Alertmanager v2 posting, and localhost metrics; build and install
    playbooks (`app/build-ids-forwarder.yml`,
    `monitoring/install-ids-forwarder.yml`).
  - Remaining: source builds and per-host rollout for Tetragon and CrowdSec,
    policy/scenario pinning, tuning window.
- Define the detection rule set and tune it to this cluster's baseline before
  enabling alerts; default signatures will be noisy against the inference and
  container workloads.
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

- Which malicious-user paths matter most in practice: guest-to-guest traffic,
  abuse of operator credentials, or misuse of the Ansible/agent access path?
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
