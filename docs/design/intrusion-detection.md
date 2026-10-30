# Intrusion detection system

## Status

**Proposed — revised 2026-10-25 after an attack-surface review of the IDS
components themselves.** The earlier Wazuh-first recommendation is withdrawn.
Researched against Wazuh 4.14.9, Tetragon 1.7.1, Tracee 0.24.1, CrowdSec 1.8.1,
Suricata 8.0.7 (Ubuntu 24.04 ships 7.0.3), Zeek 9.0.0, Falco 0.45.1, osquery
5.24.0, Arkime 6.9.1, Snort 3.12.4, and the Ubuntu 24.04 package archive.
This document turns the open questions in
`todo/security/intrusion-detection.md` into a concrete design; the TODO remains
the implementation tracker.

## Decision

Adopt a Go-first design:

1. **Tetragon** on every Linux cluster node for host runtime, file, network and
   capability observability. Run it observe-only. The control plane is Go; the eBPF
   programs are C.
2. **CrowdSec** on nodes that produce authentication and HTTP logs for log-based
   detection (SSH brute force, nginx abuse, Authentik failures). Run it in local-only
   mode with no console enrollment, no community blocklist, and no bouncers.
3. A small **`ids-forwarder`** Go service on each host. It tails the Tetragon
   JSON events file (gRPC unix socket reserved for operator tooling), accepts
   CrowdSec notification-http POSTs on localhost, maps events to Alertmanager's
   `v2/alerts` API, and persists a bounded local spool plus a Tetragon replay
   cursor.
4. **No Wazuh.** Wazuh is a C/C++/Python manager plus a root agent, with a large
   parser and command-execution surface. Its recent critical advisories make it a poor
   fit for a critical system.
5. **No auditd, AIDE, Falco, osquery, Zeek, Snort, Arkime or ntopng as core
   sensors.** They are C/C++ and/or duplicate the same surface.
6. **Suricata is optional and isolated.** If deep packet inspection is required,
   run it in a dedicated sensor VM or container with no host mounts, no cluster
   credentials and no inline IPS. If the no-C/C++ rule is absolute, skip it and accept
   reduced network coverage.
7. **Alert-only.** No automatic firewall, account or VM action.

The design deliberately has no central manager. Each host runs its own sensors and a
small Go forwarder; Alertmanager remains the only routing and deduplication layer.

## Goals

- Detect external attackers and malicious users, including a compromised
  operator/agent credential and a VM owner attacking a neighbour.
- Run a sensor on every cluster node, including remote nodes that are often offline,
  not only on core nodes.
- Run an in-cluster network sensor on the ingress path; do not assume a managed
  switch can mirror traffic.
- Route findings through the existing Alertmanager → ntfy path with a documented
  severity mapping.
- Keep user IP addresses only as long as the existing nginx/SSH access logs.
- Make coverage loss visible: a host that stops reporting is an alert, not a silent
  blind spot.
- Treat the IDS itself as part of the attack surface.

## Non-goals

- Admin laptops are out of scope for now. They are not cluster nodes and do not gate
  this rollout.
- No full packet capture archive. Packet capture is on-demand and time-boxed.
- No automatic blocking or active response in phase 1.
- No guest-internal monitoring of user VMs or containers unless an agent is
  deliberately installed inside the guest.
- No replacement for nginx, sshd, journald or the existing access logs.
- No Wazuh indexer, dashboard, or OpenSearch-derived stack.

## Constraints

- **No prebuilt third-party binaries.** Tetragon is built from source at
  `v1.7.1`; CrowdSec is built from source at `v1.8.1`; the forwarder is in-repo
  Go. No upstream apt repository and no release tarball. Suricata, if used, comes
  from Ubuntu 24.04 apt (7.0.3) or a source build at `suricata-8.0.7`.
- **Prefer memory-safe user-space code.** Go for the control plane, log parsing
  and alert forwarding. eBPF C is confined to kernel programs, not a user-space
  parser of untrusted network payloads.
- **Per-host sensors are required.** Every Linux cluster node runs Tetragon and the
  forwarder. Remote nodes are not an exception; they push over WireGuard and buffer
  locally.
- **In-cluster network sensor.** Tetragon network observability runs on nodes that
  carry ingress traffic. Router-side mirroring is not assumed.
- **No user IP retention beyond the access logs.** Any IDS-owned copy of a client or
  VM-tenant IP expires on the nginx/SSH access-log schedule. Longer-lived records
  are IP-free aggregates.
- **Alert-only.** No firewall, account or VM mutation from an alert.
- **Bounded resources.** No sensor writes to the storage root filesystem; local
  buffers are bounded by size and time; capture cannot fill a volume.
- **Remote-host reality.** Sensors and the forwarder run locally and push to
  Alertmanager over WireGuard. There is no central manager to compromise.
- **Guest boundary.** Host sensors see host activity and VM traffic, not activity
  inside a guest OS. That limitation is explicit.

## Threat model

- **External attackers.** Someone probing or exploiting the public ingress, the
  WireGuard endpoint, or an unexpectedly reachable service. Assume a perimeter
  control can be misconfigured.
- **Malicious users.** A VM owner attacking a neighbour or escaping a workload, a
  stolen SSH/WireGuard key, a compromised operator or agent credential, or an insider
  running commands they should not. Detection must cover authenticated actions, so edge
  logs alone are insufficient.
- **The IDS itself.** An attacker who can reach a sensor, feed it malicious input, or
  steal its credentials can turn detection into a pivot. The IDS is in scope as a
  target, not just as an observer.

## IDS components are attack surface

An IDS is not a passive observer. It usually has one or more of:

- a privileged host process that reads kernel events, files and logs;
- a network listener for agents, enrollment, gRPC or a web API;
- a parser for attacker-controlled logs, protocol metadata or rule files;
- a rule/plugin mechanism that can execute code;
- an active-response path that can run commands as root.

Every one of those is a potential entry point. A memory-safety bug in a parser can
turn a malicious log line into code execution on the host. A central manager is
worse: one compromise can affect every agent. The design therefore minimizes
privileged parsers, avoids a central C/C++ manager, and keeps every listener on a
unix socket or localhost.

### Wazuh review

Wazuh is a useful product, but it is a poor fit for this constraint:

- **Manager.** Listens on the agent protocol (1514), enrollment (1515), REST API
  (55000) and cluster port; parses agent-supplied events and logs; can write
  cluster files and execute active response.
- **Agent.** Runs as root on every monitored host, reads logs and files, runs SCA
  command policies, and can execute upgrade commands.
- **Language.** The main repo is C++ and C, with Python modules. The indexer and
  dashboard are separate Java and TypeScript projects.
- **Advisory history.** The `wazuh/wazuh` GitHub repository had 89 published
  security advisories as of 2026-10-25. Recent examples include:
  - CVE-2026-74045: heap buffer overflow in `wazuh-analysisd`
    `fim_generate_alert()` lets an enrolled agent corrupt manager heap memory.
  - CVE-2026-74040: a cluster worker can overwrite arbitrary master files,
    including `ossec.conf`, leading to root RCE on the manager and every agent.
  - CVE-2026-74036: remote code execution as root on Wazuh agents via SCA
    command policies.
  - CVE-2026-74041: case-sensitivity mismatch bypasses remote-command
    validation, allowing command execution as root.
- **Verdict.** Reject as the centerpiece. If Wazuh is ever retained, it must run
  in a dedicated VM with no host mounts, no indexer/dashboard, no active response,
  and a localhost-only API. Even then, the root agent remains on every host.

This is not an argument that C/C++ is always wrong. It is an argument that a
large C/C++ parser with root execution and a central manager is the wrong place to
put the cluster's detection trust boundary.

### Advisory history of the chosen pieces

Checked 2026-10-25 against each project's GitHub security advisories:

| Component | Published advisories | Chosen pin | Assessment |
|---|---|---|---|
| Tetragon | 3 | 1.7.1 | All three are fixed by 1.7.1. The high one (CVE-2026-65960) is an unauthenticated TCP gRPC admin API reachable from hostNetwork pods; the design uses a unix socket only. |
| CrowdSec | 4 | 1.8.1 | All four are fixed by 1.8.1. They are DoS issues in the Kubernetes audit webhook, HTTP acquisition, AppSec and LAPI gzip paths. The design disables the first three and binds LAPI to localhost. |
| Tracee | 0 | 0.24.1 | No published advisories. Kept as the alternative to Tetragon. |
| Suricata | 100+ (API page capped) | 8.0.7 if used | A full page of advisories, including a critical DoH2 type-confused free. This is why Suricata is optional and isolated, not a per-host critical sensor. |
| `ids-forwarder` | 0 | in-repo | New stdlib-only Go service; run `govulncheck` in CI. |

The point is not that Tetragon and CrowdSec have clean histories. They have
smaller histories, the advisories are patched at the chosen pins, and the design
disables or isolates the affected surfaces. Suricata's history is the strongest
argument against running it as a privileged per-host sensor.

## Component survey

### Host-based components

| Component | Version | In noble | Language | License | Fit | Decision |
|---|---|---|---|---|---|---|
| Tetragon | 1.7.1 (upstream) | no | Go control plane, C eBPF | Apache-2.0 | Process/file/network/capability/FIM observability; privileged; BTF | Adopt |
| CrowdSec | 1.8.1 (upstream; noble 1.4.6) | yes | Go | MIT | Log behaviour engine, local API, bouncers | Adopt local-only |
| Tracee | 0.24.1 (upstream) | no | Go, small C eBPF | Apache-2.0 | Runtime security and forensics; alternative to Tetragon | Alternative |
| Wazuh agent/manager | 4.14.9 (upstream) | no | C++/C/Python | GPLv2 | FIM/log/SCA/rootcheck; large parser and root agent | Reject |
| Falco | 0.45.1 (upstream) | no | C++ | Apache-2.0 | Runtime rules; C++ core | Reject |
| auditd | 1:3.1.2 (noble) | yes | C | GPLv2/LGPL | Kernel audit source; C userspace | Reject |
| AIDE | 0.18.6 (noble) | yes | C | GPLv2 | Periodic FIM baseline; C | Reject |
| osquery | 5.24.0 (upstream) | no | C++ | Apache-2.0 OR GPL-2.0 | Host inventory/FIM; not a detection engine | Reject |
| OpenSCAP | 1.3.9 (noble) | yes | C | LGPLv2.1 | Compliance scanning | Reject |
| YARA | 4.5.0 (noble) | yes | C | BSD-3-Clause | Signature scanning | Optional isolated |

### Network components

| Component | Version | In noble | Language | License | Fit | Decision |
|---|---|---|---|---|---|---|
| Tetragon network observability | 1.7.1 (upstream) | no | Go + C eBPF | Apache-2.0 | TCP connect/sendmsg/close, process context; no DPI | Adopt |
| CrowdSec log parsing | 1.8.1 (upstream) | yes | Go | MIT | SSH/HTTP/auth log behaviour; no DPI | Adopt local-only |
| Suricata | 8.0.7 (upstream; noble 7.0.3) | yes | C | GPLv2 | Deep packet inspection; C parser of attacker traffic | Optional isolated |
| Zeek | 9.0.0 (upstream) | no | C++ | BSD-3-Clause | Rich protocol logs; duplicate packet processing | Reject |
| Snort 3 | 3.12.4 (upstream; noble 2.9.20) | no | C++ | GPLv2 | IDS/IPS; C++ parser | Reject |
| Arkime | 6.9.1 (upstream) | no | C/Node | Apache-2.0 | Full packet capture; IP retention conflict | Reject |
| ntopng | 5.2.1 (noble) | yes | C++ | GPLv3 | Flow dashboards; not an IDS | Reject |

### Rule and content sources

| Source | License | Use | Decision |
|---|---|---|---|
| Tetragon policy library | Apache-2.0 | Process/file/network/capability policies | Adopt pinned |
| CrowdSec scenarios | MIT (hub content varies) | SSH/HTTP/auth behaviour | Adopt pinned, review each scenario |
| Sigma | DRL 1.1 | Rule source for new detections | Port rules into the forwarder, named after the upstream rule |
| Falco rules | Apache-2.0 | Rule source for new detections | Port rules into the forwarder, named after the upstream rule |
| ET Open | MIT | Suricata rules, if Suricata is used | Optional pinned |

## Why Tetragon

Tetragon is the best fit for a Go-first host sensor:

- The control plane is Go; the eBPF programs are C, loaded and verified by the
  kernel. It is not a user-space C parser of attacker-controlled network payloads.
- It observes process execution, file access, network connect/sendmsg/close,
  capabilities, namespace access and kernel module operations.
- It exports JSON events and Prometheus metrics, and has a gRPC API.
- It can run without Kubernetes, as a container or package/systemd service.
- It requires BTF and a privileged daemon; Ubuntu 24.04 provides
  `/sys/kernel/btf/vmlinux`.
- It is Apache-2.0 and actively maintained.

The limits are real: it is privileged, its eBPF code is C, its non-Kubernetes host
mode is less mature than its Kubernetes mode, and it does not provide deep packet
inspection. Those limits are why it runs observe-only, with the gRPC API on a unix
socket and no TCP listener.

## Why CrowdSec

CrowdSec is the best fit for log-based detection:

- It is Go, MIT, and packaged in Ubuntu 24.04.
- It parses sshd, nginx and application logs with behaviour scenarios.
- It exposes a local API and Prometheus metrics.
- It can run without the CrowdSec console or community blocklist.
- It is not a FIM or process sensor; it complements Tetragon.

Run it as a dedicated non-root user with read-only log access, bind the local API
to localhost, disable bouncers, disable console enrollment, and disable the central
API. Its local database retention is capped at the access-log window.

## Why not Wazuh, Falco, auditd, AIDE, osquery, Zeek, Snort, Arkime

- **Wazuh** is a large C/C++ parser with a root agent and a central manager. Its
  recent critical advisories include agent-to-manager heap corruption and root RCE
  paths. Reject as the centerpiece.
- **Falco** is C++ and overlaps with Tetragon's process/file/network surface, so it
  is not adopted as a sensor. Its rule set is used as a detection source; see
  "The behavioural-rule gap".
- **auditd** is C userspace. Tetragon covers the process/file events without a
  second kernel audit consumer.
- **AIDE** is C and periodic. Tetragon FIM is real-time; add a small Go baseline
  hasher only if periodic hash manifests are required.
- **osquery** is C++ and is a query tool, not a detection engine.
- **Zeek, Snort and Arkime** are C/C++ parsers of attacker traffic and/or full
  packet capture. They conflict with the language and retention constraints.
- **ntopng** is C++ and is a flow dashboard, not an IDS.
- **OpenSCAP and YARA** are C tools for compliance and signatures, not the
  detection loop.

## The behavioural-rule gap

The component survey above rejected Falco on language and duplication grounds:
a C++ core, a second privileged syscall sensor, and no package in noble. Those
reasons still hold and the sensor choice is unchanged. What the survey did *not*
evaluate is the **rule language**, and the tuning work showed that is the piece
this design actually lacks.

Tetragon's `TracingPolicy` is a matching language. It selects syscalls and
arguments. Tetragon 1.7 added `matchParentBinaries`, so a policy can match the
immediate or, with `followChildren`, a transitive parent binary. That covers
some single-event rules, but it requires `--parents-map-enabled` (a bounded BPF
map, 32768 entries by default), matches only the binary path and not the
parent's arguments, and has an open setup bug (`cilium/tetragon#5069`; a fix is
proposed in `#4888` but not merged as of 2026-10-28), so it is not yet a
dependable foundation. It still has no rule composition and no notion of a
detection with a name and a rationale. A single event can sometimes be
approximated now — "a shell was spawned by a service" is a parent-binary
selector — but "a service spawned a shell and then made an outbound connection"
is a sequence, and "a sensitive file was read by something that is not a
package manager" is a list-based predicate with exceptions, neither of which a
`TracingPolicy` can express. The plan in the survey assumed the policy
library plus Sigma used offline would be enough; it was not, and the shortfall
was discovered by writing the matching logic by hand in `ids-forwarder`, one
noise source at a time.

That hand-written layer is what Falco and Sigma already ship: macros over
process name and parent, container awareness, and a decade of tuning against
exactly the noise this fleet produced (container health probes, collector
wrappers, package-manager queries, control-plane service pairs). Re-deriving
them locally loses that provenance and makes every rule locally un-reviewable
against a known reference.

**Decision: keep the pipeline, borrow the rule layer.**

- Detections in `ids-forwarder` are named after the Falco or Sigma rule they
  implement, and the design records that mapping. A rule that cannot be named
  after an upstream rule is not added.
- Suppression stays local, because it encodes this cluster's traffic — which
  Ceph port is expected, which container bridge is local. That knowledge has no
  upstream equivalent and is correctly ours.
- Reopening the stack question is justified when the named watch list outgrows
  roughly a dozen rules, or when a required detection needs composition the
  forwarder cannot express. At that point the candidates are Sigma rules on
  RSigma (keeps the sensor) or Falco (a second sensor or a sensor swap); see
  the off-the-shelf survey below.

Two pipeline gaps are independent of this decision and worth fixing first:

- The `execBinaries` allowlist drops every non-shell exec before any rule sees
  it, so a service executing a binary directly is invisible. A rule engine
  given the same truncated stream would have the same blind spot.
- Identity attributes cannot substitute for behaviour. `auid` is unset for
  anything systemd started, is stamped by `pam_loginuid` for cron jobs (so a
  root cron job looks like a root login), and persists on daemons started from
  a shell. It is useful as a label on stored detections, not as a filter.

### Off-the-shelf rule-layer options (2026-10-28)

No project is a drop-in Falco rule layer for Tetragon events. The credible
options, checked on 2026-10-28, are:

| Option | Keeps Tetragon | Rule format | Upstream content | Language | Main cost |
|---|---|---|---|---|---|
| Tetragon `matchParentBinaries` | yes | `TracingPolicy` | policy library only | Go + C eBPF | binary-only parent match; open bug `#5069`; no composition |
| Falco, plugin-only (`collector` + `json`) | yes | Falco rules over `json.value[...]` | Falco rules must be rewritten for the JSON schema | C++ core, Go plugins | unprivileged, but no upstream rules as-is; `collector` is v0.1.1 |
| Falco as a second sensor | no | Falco rules | `falcosecurity/rules` | C++ | second privileged syscall consumer; contradicts the sensor decision |
| Tracee, sensor swap | no | Go signatures, rebuilt into Tracee | 30 documented built-in signatures | Go + C eBPF | loses Tetragon tuning and kernel-side filtering; custom rules are code |
| Sigma + RSigma | yes | Sigma YAML | 141 SigmaHQ Linux rules (4 log-source categories, r2026-07-01) | Rust | new daemon; young engine; needs a Tetragon field-mapping pipeline |
| Sigma + a Go library (`sigmalite`, `go-sigma-rule-engine`) | yes | Sigma subset | depends on library | Go | embeddable, but no correlation/full Sigma coverage |

**Recommendation: pilot Sigma rules on RSigma, not Falco.** It is the only
option that keeps Tetragon and the forwarder, runs unprivileged and
memory-safe in user space, and brings an upstream Linux rule library with
correlation, linting and backtesting. The engine is `timescale/rsigma` (Rust,
MIT); the rules are `SigmaHQ/sigma`. A field-mapping pipeline maps the
normalized event fields (`Image`, `CommandLine`, `ParentImage`,
`ParentCommandLine`, `User`, `EventType`, destination address and port) onto
the Sigma schema. The forwarder already has the parent process block in each
Tetragon event, so this does not depend on the broken
`matchParentBinaries` path.

The caveats are material and are why this is a pilot, not a decision to
replace the Go rules yet:

- RSigma is young: first release 2026-02, v0.25.0 on 2026-10-19, 177 GitHub
  stars. It published five advisories in that window (three high); all are
  fixed by v0.25.0 (two at v0.23.0). The pilot must pin `>= 0.25.0`, disable
  the HTTP event input, OTLP, MCP, enrichers and outbound webhooks, and keep
  only a unix-socket input and a loopback management API.
- SigmaHQ's Linux coverage is process-centric: 122 `process_creation`, 8
  `file_event`, 5 `network_connection` and 6 `auditd` rules at
  `r2026-07-01`. Tetragon FIM events need argument mapping before the
  `file_event` rules can be used; start with `process_creation` and
  `network_connection`.
- The engine is a second per-host process. Measure CPU, RSS and detection
  latency on a storage node and a GPU node before fleet rollout.
- A rule that exists only in Falco still has to be ported to Sigma once. The
  naming/provenance rule above does not change.

Pilot shape: run RSigma as a hardened systemd service under the same
unprivileged user as `ids-forwarder` (RSigma restricts its unix input socket to
mode `0600`), rules and pipelines read-only under `/etc`, SQLite state under
`/var/lib`, detections in a file sink with external rotation; feed it
normalized NDJSON over a unix socket from `ids-forwarder`; keep the forwarder
as the only Alertmanager client and spool owner. Compare its detections with the current hand-written rules
for two weeks on a BTF-capable VM or physical host. Adopt only if it catches
the same events with acceptable noise and resource use.

**Spike result (2026-10-28).** The offline harness at
`ids-forwarder/sigma-spike/` normalizes synthetic Tetragon `process_exec`
events and evaluates them with RSigma 0.25.0. Eight curated process rules
caught all eight malicious samples and none of the six benign controls; five
network rules were added after the guarded replay below.
The full 122-rule `process_creation` set added two benign matches (`File
Deletion` on `rm -f /tmp/scratch`, `Curl Usage on Linux` on a plain HTTPS
request) and duplicated two malicious events with generic rules, so the pilot
must start from a curated subset and tune against a benign baseline.
Rule loading was under 0.05 s; 10,000 events took 0.08 s and 16.6 MB max RSS
with the 13 curated rules, and 0.28 s and 19.5 MB with 122 rules. The daemon also
accepted NDJSON on a unix socket, wrote a file sink, and persisted SQLite
state locally. The Incus dev nodes cannot run Tetragon itself: they lack
`/sys/kernel/btf/vmlinux` and have `kernel.unprivileged_bpf_disabled=2`. A
real sensor-to-rule end-to-end test needs a VM or a physical host.

CVE check (2026-10-28): the SigmaHQ rule repository has no published security
advisories. The separate pySigma tooling has five advisories, fixed in
2.0.0/2.0.1; it is not used by RSigma, but pin `pySigma >= 2.0.1` if
`sigma-cli` is used for conversion or validation. RSigma has five GitHub
advisories, all fixed by 0.25.0. Its
upstream 0.25.0 lockfile still resolves four vulnerable crates in the Linux
release graph: `ctutils` 0.4.2 (`RUSTSEC-2026-0385`), `rustls-webpki` 0.103.15
and `webpki-roots` 1.0.9 (`CVE-2026-111943`), and `tokio` 1.53.2
(`RUSTSEC-2026-0386`). The pilot has no outbound TLS and does not call
`AsyncFd`, so the TLS and I/O-safety paths are not reachable, but the lockfile
must be patched before deployment. Updating to `ctutils` 0.4.4,
`rustls-webpki` 0.103.16, `tokio` 1.53.3 and `webpki-roots` 1.0.10 builds
cleanly, reduces the release graph to zero OSV advisories, and still passes
the spike.

Two production replays exercised the curated set on real output. A
~27-minute storage-leader window had 37,956 `process_exec` and 13,758
normalized `tcp_connect` events; a GPU-node window had 12,486 `process_exec`
and 150 `tcp_connect` events. The 13 curated rules plus a local filter for
routine root interactive shells produced zero detections in both windows.
Synthetic positive controls on the real streams produced one webshell
detection and one reverse-shell detection, so the process and network paths
work. Before the filter, the storage window had one and the GPU window had two
`Bash Interactive Shell` hits, all root with no parent binary. The full
122-rule process set produced 7,019 detections in the storage window and 1,498
on the GPU node, dominated by routine `ip`, `hostname`, `curl` and `findmnt`
discovery rules. Five SigmaHQ `network_connection` rules initially produced
199 false positives, all on `process_exec` events with no `DestinationIp`; a
per-category `EventType` guard reduced that to zero. The evaluator does not
enforce `logsource.category`, so every category needs its own guard. The
curated set is quiet on these two windows; the next gate is a two-week
BTF-capable host run, not more offline replay.

Fallbacks if the pilot fails:

- Falco's `collector` and `json` plugins can run Falco as an unprivileged
  userspace rule engine over Tetragon JSON. This is the most mature rule
  language, but every rule must be rewritten against JSON pointers, so the
  upstream Falco content is not reusable as-is.
- A Tracee sensor swap is justified if the host-mode/resource risk of Tetragon
  becomes the larger problem. It is the lowest-advisory option (zero published
  advisories) and has a built-in signature library, but it is a sensor swap,
  not a rule-layer addition.
- Do not adopt Tracee only for its signatures while keeping Tetragon: the two
  sensors would duplicate the same kernel surface.

## Architecture

```
per Linux cluster node
  Tetragon (Go control plane, eBPF C)
    ├── gRPC unix socket only (/var/run/tetragon/tetragon.sock)
    ├── JSON events to a bounded local file
    └── Prometheus metrics on localhost:2112

  CrowdSec (Go, local-only)
    ├── reads sshd/nginx/auth logs read-only
    ├── local API on localhost
    └── local DB retention capped at the access-log window

  RSigma (Rust, Sigma rule engine)
    ├── normalized NDJSON in on a unix socket (/run/rsigma/events.sock)
    ├── detections out to a file sink (/var/log/rsigma/detections.ndjson)
    └── loopback management API and metrics (127.0.0.1:9290)

  ids-forwarder (Go, in-repo)
    ├── tails Tetragon JSON events file; gRPC unix socket reserved for ops
    ├── copies a bounded, normalized stream to RSigma (never blocks paging)
    ├── tails the RSigma detection sink with a durable byte cursor
    ├── optional localhost HTTP receiver for CrowdSec notification-http
    ├── maps to Alertmanager v2/alerts
    ├── durable cursor; replays rotated Tetragon files after restart
    ├── bounded fsynced spool; backpressure instead of dropping
    └── pushes over WireGuard; no central manager

Alertmanager (existing HA)
  └── ntfy-webhook (existing)
```

The Sigma path is additive. `ids-forwarder` only copies events into the
RSigma socket when `-sigma-socket` is set; a missing or slow engine increments
drop/error counters and cannot backpressure Tetragon tailing or the
Alertmanager spool. Detections are mapped back to normal `IDSAlert` records,
so redaction, dedup, spooling and replay stay in one component. Both services
run as the `ids-forwarder` user because RSigma restricts its input socket to
mode `0600`. The service is deployed to the `managed` group; the go/no-go
gate is a two-week baseline against the hand-written rules.

### Placement

| Node class | Tetragon | CrowdSec | Forwarder | Notes |
|---|---|---|---|---|
| Core (`s1`–`s3`) | yes | yes | yes | no central manager |
| Storage (`s4+`) | yes | yes | yes | ingress logs on the storage leader |
| Compute (`c*`) | yes | yes | yes | observe-only |
| Nvidia (`nv*`) | yes | yes | yes | observe-only |
| NAS (`nas*`) | yes | yes | yes | low traffic |
| Frontend (`f*`) | yes | yes | yes | public ingress logs |
| macOS compute (`m*`) | phase 2 | phase 2 | phase 2 | separate build |
| Adhoc (`x*`) | phase 2 | phase 2 | phase 2 | dynamic inventory |
| Admin laptops | no | no | no | out of scope |

Phase 1 starts with core, storage and frontend. Compute, Nvidia, NAS and
remote nodes follow after the CPU/memory budget is measured on the dev cluster.
The target remains every Linux cluster node; the phase boundary is not an exclusion.

### Tetragon

- Run as a systemd service built from source at `v1.7.1`.
- Observe-only. Do not enable enforcement.
- gRPC on the unix socket only. Do not enable a TCP listener. If a TCP listener
  is ever required, require mTLS and restrict it to WireGuard.
- Metrics on `localhost:2112`, scraped by the existing Prometheus.
- JSON events written to a bounded local file with rotation. The forwarder
  tails that file; the gRPC unix socket stays reserved for operator tooling.
- Retention matches the access-log window but is volume-dependent: Tetragon
  rotates at 24h **or** 50 MB, whichever comes first, and keeps seven
  compressed backups. Under heavy event volume the size cap dominates, so
  the retained window can be well under a day. Do not assume a daily file.
- Policies pinned in Ansible: file writes to `/etc` and systemd paths,
  targeted root uid/gid transitions and capability gains, actual kernel
  module loads, and non-loopback TCP connects. Shell and package-manager
  execs are already exported by the base sensor's `process_exec` events and
  are filtered by the forwarder instead. `PROCESS_EXIT` is not exported
  because the forwarder does not consume it.
- Health server on `localhost:6790`: the default `:6789` collides with the
  Ceph monitor.

### CrowdSec

- Build from source at `v1.8.1`; no upstream apt repository.
- Run as a dedicated user with read-only access to sshd/nginx/auth logs.
- Local API bound to localhost; no public listener.
- No console enrollment, no central API, no community blocklist, no bouncers.
- Pin and review scenarios; do not auto-update from the hub at runtime.
- Cap the local database retention at the access-log window.

### Forwarder

- Small Go service in the repo, stdlib only.
- Tails the Tetragon JSON events file; optional localhost HTTP receiver for
  CrowdSec notification-http POSTs.
- Every suppression entry must correspond to a producer the cluster does not
  own. Cluster-owned units use native systemd directives instead of shell
  wrappers (`ConditionPathIsMountPoint` rather than a `mountpoint` probe,
  direct `ExecStart` rather than a pipeline), so a recurring shell exec is
  either fixed at the source or visible as an alert. Producer-side shell
  removal is the preferred fix; filters are for packaged software.
- Forwards `process_kprobe` events plus `process_exec` events for an
  allowlist of binaries (interactive shells, privilege tools, package
  managers, downloaders). Other execs are dropped in-process: the base
  sensor emits one event per exec and that volume is not useful as alerts.
  The forwarder also suppresses recognized loopback health probes,
  container health checks, node-exporter collectors, login MOTD generation,
  Ansible connection probes, package-architecture queries, expected
  private-network TCP service/port pairs, the exact gateway-health probe to
  its public endpoint, systemd's transient `/proc/self/fd/9` privilege
  transitions, sysstat's `debian-sa1` cron wrapper, the temp file of the
  managed blackbox-target rewrite, the leader's healthchecks.io heartbeat,
  and the exact `ycluster wg reconcile`
  write to its managed WireGuard config, counted by reason in
  `ids_forwarder_suppressed_total`. The underlying events remain in the
  Tetragon log. Shell execs and every other writer or file path remain
  alertable. TCP summaries drop per-connection socket fields (source port,
  cookie, state) so repeat connects to the same destination deduplicate.
  The heartbeat is matched on its script path rather than its destination:
  its failure mode is an unanswered check on the far side, and a repointed
  URL is visible only through the write to its environment file, which is
  mode 0640 because the ping URL is a bearer token.
- Maps severity and rule metadata to Alertmanager `v2/alerts`.
- Appends every classified detection to `detections.jsonl` in the forwarder
  state directory, one JSON object per line, rotated by size. Alertmanager is
  the paging path only: it keeps minutes of state and has no query, so the
  history of what the sensor classified belongs in this file. Records carry
  time, host, source, rule, severity, layer and the already-redacted summary;
  suppressed events are counted in `ids_forwarder_suppressed_total` instead
  of being written. The same shape can later be shipped to a central store
  without changing the forwarder.
- Posts to every core Alertmanager instance over the cluster network, in
  order, and stops at the first that accepts the alert. Non-core nodes have
  no local Alertmanager.
- Persists the exact last Tetragon line processed (event time plus a content
  hash), not a timestamp watermark. Tetragon can write events with older
  timestamps, and a watermark would discard them. On restart the forwarder
  finds that line in the current file or the rotated gzip backups, forwards
  everything after it in scan order, and resumes live tailing after skipping
  the already-replayed prefix. A fresh install replays a bounded startup
  grace (one minute) so events written while an old process was stopping are
  not silently skipped.
- If the boundary line is no longer in the retained files, the forwarder
  replays everything retained and emits `IDSForwarderGap` once per episode.
  A corrupt or unreadable cursor is treated the same way instead of
  restarting at EOF.
- Persists redacted alerts in a bounded local spool before advancing the
  cursor. The spool is fsynced, acknowledged only after Alertmanager accepts
  the alert, and applies backpressure when full instead of dropping the
  oldest entry. A crash can replay an alert; it cannot silently lose one.
  Acks are replaced atomically and carry a content anchor, and compaction
  moves the ack to the replacement before swapping the file, so a crash in
  either window replays rather than skips. Entries above 4 MiB are dropped
  and counted in `ids_forwarder_oversized_total` so one pathological event
  cannot wedge the queue, and a record Alertmanager rejects with a
  non-retryable 4xx is dropped and counted in
  `ids_forwarder_rejected_total` instead of blocking every later alert. A
  spool read or write failure makes the process exit non-zero so systemd
  restarts it; the localhost receiver answers 503 and also exits for the
  same reason. `-cursor-file` is rejected without `-spool-file`, because an
  in-memory queue may drop an alert the cursor has already passed. A cursor
  that cannot be persisted is not fatal (the spool is still durable) but is
  counted in `ids_forwarder_cursor_errors_total`, because a restart would
  replay from an older boundary.
- Refreshes `EndsAt` at delivery so a queued alert's active window starts
  when it is sent. A separate `-max-event-age` cutoff (24h by default)
  prevents very old events from paging; it is not the alert TTL.
- Emits `ids_forwarder_up`, `ids_forwarder_queue_depth`,
  `ids_forwarder_spool_bytes`, `ids_forwarder_dropped_total`,
  `ids_forwarder_expired_total`, `ids_forwarder_gap_total`,
  `ids_forwarder_oversized_total`, `ids_forwarder_rejected_total`,
  `ids_forwarder_cursor_errors_total`, `ids_forwarder_forwarded_total`,
  `ids_forwarder_deduped_total` and
  `ids_forwarder_suppressed_total{reason=...}`.
- Does not copy raw Tetragon events into the spool and does not put IPs in
  labels, annotations, or the spool.
- Coverage is republished through the node_exporter textfile collector
  (`ids_component_up{component=...}`) so core Prometheus can alert on a
  stopped sensor on any node without opening the loopback metrics endpoints.

### Network sensor

Tetragon network observability covers TCP connect/sendmsg/close with process
context. It does not inspect payloads. CrowdSec covers HTTP and SSH behaviour from
nginx/sshd logs. This is the phase 1 network surface.

If deep packet inspection is required, Suricata runs in a dedicated Incus VM on the
ingress host:

- no host mounts and no cluster credentials;
- a dedicated capture interface;
- no inline IPS mode;
- EVE JSON with payload, HTTP body and file extraction disabled;
- alerts shipped to the forwarder over the sensor VM's WireGuard address.

Treat that VM as untrusted. A compromise must not yield cluster credentials or a
host mount. If the no-C/C++ rule is absolute, skip Suricata and accept the reduced
network coverage.

## Detection content

- **Tetragon policies.** Writes below `/etc` and systemd unit paths,
  targeted root uid/gid transitions and capability gains, actual kernel
  module loads, and non-loopback TCP connects. The generic
  `commit_creds`, broad `security_capset`, and module-request hooks are
  deliberately excluded: they match normal credential commits and netdev
  autoload requests, not escalation. High-volume compiled control-plane
  clients (`etcdctl`, `etcd`, Prometheus, and `local-ai-proxy`) have their
  expected private-network port pairs filtered in the kernel; unexpected
  ports and external destinations still generate events. Container bridges
  (`172.16.0.0/12`) are excluded alongside loopback: they are host-local,
  and a container reaching another node appears with the cluster or public
  destination address. Root uid/gid
  transitions made by `sshd` and cron are also filtered in the policy.
  systemd's transient re-exec presents as `/proc/self/fd/9`, which
  `matchBinaries` cannot resolve, so that single path remains visible and is
  suppressed by the forwarder. Every other process raising to root remains
  event-generating. The Ceph and `ycluster` CLIs are Python scripts, so
  `matchBinaries` does not identify them reliably; their expected pairs
  remain visible and are suppressed by the forwarder.
- **Tetragon process execs.** The forwarder forwards `process_exec` only for
  shells, privilege tools (`sudo`, `su`), package managers and downloaders.
  The binary allowlist is a playbook variable
  (`ids_forwarder_exec_binaries`). Recognized health probes and collector
  scripts are suppressed before alert creation.
- **Tetragon FIM.** Real-time file access and modification on `/etc`, systemd unit
  paths, SSH config, cron paths and Ansible-managed config directories. This is
  access/modification detection, not a periodic hash baseline.
- **CrowdSec scenarios.** sshd authentication failures and successes, sudo
  failures, nginx 4xx/5xx bursts, scanner paths, and Authentik authentication
  failures.
- **Baseline hashes.** If a periodic hash manifest is required, add a small Go
  hasher that writes a signed manifest and compares it on a timer. Do not add AIDE.
- **Sigma.** Use as an offline source for new Tetragon/CrowdSec rules; no runtime
  Sigma engine.
- **Tuning.** Run the full policy/scenario set in the dev cluster for two weeks,
  suppress expected package updates, Ansible runs, container builds and health probes,
  then enable alerts on core, storage and frontend first. The first production
  pass targets raw event volume: process exits are not exported, routine exec
  probes are filtered, and noisy credential hooks are replaced with narrow
  privilege-gain and module-load matches. The TCP policy is canaried separately
  before fleet rollout because a corrected matcher can expose a large
  connection population.

## Data and retention

- **Tetragon events.** Bounded local files; rotate at 24h or 50 MB and keep
  seven compressed backups. The effective window is the smaller of the time
  and volume caps and can be much shorter than a day under load. Network
  events contain IPs, so they are subject to the same cutoff.
- **CrowdSec database.** Cap retention at the access-log window. No console or
  community sync means no second copy leaves the cluster.
- **Forwarder cursor and spool.** The cursor is the event time and content
  hash of the last line processed, replayed in scan order; the live tailer
  resumes at the exact byte offset replay consumed in the held inode. The spool is a
  100 MB append-only JSONL file of already-redacted alerts, fsynced before the
  cursor advances. It applies backpressure rather than dropping; if Tetragon
  rotation deletes the events the cursor needed, the forwarder replays what is
  left and emits an `IDSForwarderGap` alert. No central event store: raw
  events stay on the node that produced them, and only the classified
  detection record (`detections.jsonl`) is kept beyond the sensor's rotation.
- **No IPs in labels.** Do not put client or VM-tenant IPs in Prometheus labels,
  Alertmanager labels/annotations, ntfy messages or incident notes. Use host, VM
  identity, rule ID and event type. Central Prometheus retention is 90 days, so an IP
  label would silently become a long-lived IP record.
- **Packet capture.** On-demand only, time-boxed and deleted at the access-log
  cutoff. No continuous pcap archive.
- **No restore resurrection.** A restored backup or replayed queue must not reintroduce
  IP-bearing records older than the cutoff.

## Alerting and response

- **Severity.** Map Tetragon severity/priority and CrowdSec scenario severity to
  critical, warning and info. Tune by rule group, not by a single level.
- **Labels.** `alertname=IDSAlert`, `source=tetragon|crowdsec`, `severity`,
  `host`, `layer=host|network|container`, `rule_id`. No IPs.
- **Grouping.** Group by `alertname`, `host` and `rule_id`; inhibit lower
  severity when a critical alert for the same host and rule is active.
- **Coverage alerts.** `TetragonDown`, `CrowdsecDown`, `IdsForwarderDown`,
  `IdsForwarderDropping`, `IdsForwarderBacklog`, `IdsForwarderExpired`,
  `IdsForwarderCursorFailing` and `IDSForwarderGap`. A
  host that stops reporting is an alert. Component alerts read
  `ids_component_up` from the node_exporter textfile collector so they work
  on every node, not just where Prometheus runs. `IDSForwarderGap` is sent by
  the forwarder itself when Tetragon has already deleted events the cursor
  still needed.
- **Tuning window.** Detection alerts (`alertname=IDSAlert`) route to a silent
  receiver (`ids-silent`) and are additionally silenced in Alertmanager while
  policies and scenarios are tuned against real traffic. They stay visible in
  the Alertmanager API/UI. Remove that route only when the detections are known
  to be low-noise. Coverage alerts keep their normal routing: a stopped sensor
  is a blind spot, not tuning noise. Detection events are replayed after an
  outage while they remain in the sensor log; only events older than
  `-max-event-age` are dropped, and an exhausted replay window produces a
  separate gap alert.
- **Response.** None in phase 1. No bouncers, no active response, no firewall
  change, no account disable, no VM stop. Triage is manual: inspect, isolate the host
  or revoke the WireGuard peer only with explicit approval, then rotate keys.
- **Runbook.** Add a triage section to `docs/operations/` with alert names, evidence
  to collect, and containment actions that require approval.

## Deployment

- **Build.** `build-tetragon.yml` builds Tetragon from source at `v1.7.1`;
  `build-crowdsec.yml` builds CrowdSec from source at `v1.8.1`; the forwarder is
  in-repo Go. No prebuilt binaries.
- **Rules.** Vendor Tetragon policies and CrowdSec scenarios at pinned versions.
  Do not fetch policies or scenarios at runtime.
- **Agent role.** One Ansible role installs Tetragon, CrowdSec, the forwarder,
  log rotation, local API binding and coverage metrics. It is applied to every Linux
  node, with a separate frontend play because frontends have a smaller package set.
- **No central manager.** There is no Wazuh-style manager and no RBD state. Each host
  pushes alerts to Alertmanager over WireGuard. Alertmanager remains HA.
- **Rollout.** Dev cluster first; then core, storage and frontend; then compute,
  Nvidia, NAS and remote nodes; macOS compute and adhoc nodes in phase 2.
- **Rollback.** Stop and remove the forwarder, Tetragon and CrowdSec, and leave
  existing nginx/sshd/auditd logs untouched. A rollback does not delete access logs
  or user data.

## Implementation gotchas (verified against v1.7.1 / v1.8.1)

These were found during the first real rollout and are easy to rediscover the
hard way. They apply to the pinned versions; re-verify on upgrades.

### Tetragon

- **Pin the commit SHA of a release, not the annotated tag object SHA.**
  `git checkout` dereferences annotated tags, so a SHA assertion against the
  tag object can never pass.
- **Key/value config must be `/etc/tetragon/tetragon.yaml`.** The
  `--config-dir` drop-in mechanism merges raw file contents as map values; it
  does not parse YAML keys. Only the drop-in path tolerates non-YAML files.
- Config keys: `tracing-policy-dir` (not `policies-dir`),
  `metrics-server` (not `metrics-address`). The gRPC socket address needs the
  `unix://` scheme. `PROCESS_CONNECT` is not a valid `export-allowlist`
  event-set enum in v1.7.1 — connect events arrive as `PROCESS_KPROBE`.
- The default health port `:6789` collides with the Ceph monitor. Set
  `health-server-address: 127.0.0.1:6790`.
- **Do not set `AmbientCapabilities`/`CapabilityBoundingSet`.** The systemd on
  these nodes mis-translates `CAP_BPF`/`CAP_PERFMON` and grants unrelated
  capabilities instead; the sensor then fails to pin BPF maps with EPERM. Run
  as root with full capabilities and `NoNewPrivileges=yes`. Filesystem
  sandboxing (`ProtectSystem=strict`) breaks bpffs/tracefs writes.
- `RuntimeDirectory=tetragon` is required: mount-namespace setup fails with
  status 226 if the runtime directory referenced by the unit does not exist.
- **Policies:** use cluster-wide `TracingPolicy` (the namespaced kind requires
  a namespace). Match operators are `Prefix`/`Postfix`/`Mask` (not
  `MatchPathPrefix`/`MatchPathSuffix`). One `kprobes` entry per kernel symbol
  — merge selectors instead of repeating the call. `exec` is not a policy
  field; the base sensor already emits `process_exec` for every exec.
- **Tetragon supports at most five selectors per kprobe.** Keep expected
  service filters consolidated, and remember that `matchBinaries` does not
  reliably identify interpreted scripts such as the Ceph and `ycluster`
  CLIs.
- **Ansible `copy` does not delete retired policy files.** Removing a policy
  from the source tree leaves the old file active on disk; the install
  playbook explicitly removes retired policy names after copying the
  current set, including when a stale synced source file is still present.
- The eBPF objects land in `bpf/objs/` under the source tree; the runtime
  expects them flat under `/var/lib/tetragon/`. `tetra` needs
  `--server-address unix:///var/run/tetragon/tetragon.sock`.
- **`export-file-rotation-interval` is not a retention guarantee.** The
  `export-file-max-size-mb` cap also rotates. With a noisy policy the
  current file can rotate every few minutes, so seven backups may cover only
  tens of minutes, not a week. The forwarder replays rotated `.gz` files and
  emits `IDSForwarderGap` when the cursor has fallen out of that window.

### CrowdSec

- **Bootstrap order matters.** The agent refuses to start without
  `local_api_credentials.yaml`, but `cscli machines add` writes the database
  directly. Register the machine (`--auto -f <credentials path>`) before the
  first start, then fix ownership of the credentials file and the data
  directory to the `crowdsec` user.
- The `api.client` section is mandatory even in local-only mode; removing it
  fails startup with "no API client section in configuration".
- `simulation.yaml` must exist (an empty `{}` is fine) or init is fatal.
- The notification plugin binary must be owned by the `crowdsec` user; a
  root-owned plugin is a fatal load error.
- **Parsers:** with an empty hub there is no s00-raw syslog stage, so
  `evt.Parsed.message` never exists. Grok the raw line with
  `apply_on: Line.Raw` — exactly that string. `raw`, `message` and
  `evt.Line.Raw` do not work (the last silently looks up a nonexistent
  parsed field).
- Parser files need top-level `name:`/`author:`/`description:`. Without a
  name the file loads and appears in `cscli parsers list`, but its nodes
  never run and never appear in `cscli explain`.
- expr-lang in v1.8.1: use the builtin `split(s, sep)`, not method syntax;
  ternary is `cond ? a : b`; `onsuccess: continue` (not `continue_stage`);
  use `evt.Line.Labels.type` for acquisition labels (`evt.Labels` does not
  compile). Escape regexes as `".*\\.(css|...)$"` inside double-quoted YAML.
- Ansible must escape Go-template fragments destined for CrowdSec's own
  templating: write `format: "{{ '{{.|toJson}}' }}"` for the
  notification-http body.

## Validation

- **Unit tests.** Forwarder severity mapping, deduplication, durable spool
  (torn appends, crash windows around compaction and acks, oversized entries),
  out-of-order and same-timestamp cursor replay, rotated gzip replay, gap
  detection, shutdown during a backlog, IP-field rejection, retention cutoff
  and Alertmanager payload.
- **Dev cluster.** Trigger a watched-file change, a service-user shell, a network
  connect and an SSH failure; verify one Alertmanager alert reaches ntfy and no second
  copy is stored.
- **Remote node.** Disconnect WireGuard, generate events, reconnect, and verify
  buffered events replay in order without duplicates. Restart the forwarder across
  a Tetragon rotation and verify rotated events replay; verify the gap alert when
  the cursor is older than the retained files.
- **Coverage.** Stop Tetragon, CrowdSec and the forwarder in turn; verify the
  corresponding coverage alert fires and clears.
- **Retention.** Inject a synthetic IP-bearing event, advance the retention clock, and
  verify the IDS copy is purged at the access-log cutoff.
- **Resource.** Measure CPU, memory and disk on a GPU node and a storage node; disable
  Tetragon on any node that exceeds the budget and verify the gap is visible.
- **Attack surface.** Verify the Tetragon gRPC TCP listener is closed, the CrowdSec
  local API is bound to localhost, bouncers and console enrollment are disabled,
  and the forwarder has no inbound listener.
- **Advisory pins.** Confirm at build time that the Tetragon pin is >= 1.7.1 and
  the CrowdSec pin is >= 1.8.1, and fail the build on a regression. Run
  `govulncheck` against the forwarder in CI.
- **No-block.** Verify an alert does not change firewall, accounts or VMs.

## Open questions

- Is Tetragon's eBPF C acceptable? The user-space control plane is Go, but the
  kernel programs are C. If that still violates the constraint, there is no mature
  off-the-shelf replacement with the same coverage.
- Is CrowdSec needed, or can nginx/sshd logs plus Tetragon network events cover
  the log-detection cases with a smaller Go parser?
- Is deep packet inspection required? If yes, is an isolated Suricata VM acceptable,
  or does the no-C/C++ rule exclude it?
- Is a periodic hash baseline required? If yes, build a small Go hasher rather than
  adding AIDE.
- Does Tetragon's non-Kubernetes host mode meet the stability and resource budget?
  Spike it on a storage node and a GPU node before rollout.
- Is per-host CrowdSec worth the footprint versus a single log collector on the
  storage leader? Per-host keeps remote nodes self-contained; central reduces process
  count but adds a log-shipping dependency.

## Sources

- Wazuh security advisories: <https://github.com/wazuh/wazuh/security/advisories>
- Tetragon security advisories: <https://github.com/cilium/tetragon/security/advisories>
- CrowdSec security advisories: <https://github.com/crowdsecurity/crowdsec/security/advisories>
- Tracee security advisories: <https://github.com/aquasecurity/tracee/security/advisories>
- Suricata security advisories: <https://github.com/OISF/suricata/security/advisories>
- Tetragon CVE-2026-65960 (gRPC TCP listener): <https://github.com/cilium/tetragon/security/advisories/GHSA-6qqg-3qf7-p3v5>
- Wazuh architecture: <https://documentation.wazuh.com/current/getting-started/architecture.html>
- Wazuh agent buffer: <https://documentation.wazuh.com/current/user-manual/reference/ossec-conf/client-buffer.html>
- Tetragon container install: <https://tetragon.io/docs/installation/container/>
- Tetragon events and JSON export: <https://tetragon.io/docs/concepts/events/>
- Tetragon metrics: <https://tetragon.io/docs/installation/metrics/>
- Tetragon gRPC TLS: <https://tetragon.io/docs/installation/grpc-tls/>
- Tetragon FIM: <https://tetragon.io/features/file-integrity-monitoring/>
- Tetragon network observability: <https://tetragon.io/docs/use-cases/network-observability/>
- CrowdSec overview: <https://docs.crowdsec.net/docs/next/intro/>
- CrowdSec local API: <https://docs.crowdsec.net/docs/next/local_api/intro/>
- CrowdSec community blocklist: <https://docs.crowdsec.net/docs/next/central_api/community_blocklist/>
- Tracee: <https://github.com/aquasecurity/tracee>
- Suricata EVE JSON: <https://docs.suricata.io/en/latest/output/eve/eve-json-output.html>
- Ubuntu 24.04 package versions: Launchpad `getPublishedBinaries` for `noble/amd64`
  (`crowdsec` 1.4.6, `suricata` 1:7.0.3, `auditd` 1:3.1.2, `aide` 0.18.6,
  `yara` 4.5.0, `openscap-scanner` 1.3.9, `ntopng` 5.2.1).
- Upstream release tags and licenses: GitHub release/LICENSE metadata for
  `wazuh/wazuh` (v4.14.9, GPLv2), `cilium/tetragon` (v1.7.1, Apache-2.0),
  `aquasecurity/tracee` (v0.24.1, Apache-2.0), `crowdsecurity/crowdsec`
  (v1.8.1, MIT), `falcosecurity/falco` (0.45.1, Apache-2.0),
  `osquery/osquery` (5.24.0, Apache-2.0 OR GPL-2.0),
  `OISF/suricata` (8.0.7, GPLv2), `zeek/zeek` (v9.0.0, BSD-3-Clause),
  `arkime/arkime` (v6.9.1, Apache-2.0), and `snort3/snort3` (3.12.4, GPLv2).
- Tetragon selectors, including `matchParentBinaries`: <https://tetragon.io/docs/concepts/tracing-policy/selectors/>
- Tetragon `matchParentBinaries` setup bug: <https://github.com/cilium/tetragon/issues/5069>
- Tetragon 1.7 parent-process visibility: <https://isovalent.com/blog/post/tetragon-v1.7-release>
- Falco event sources and plugin-only mode: <https://falco.org/docs/concepts/event-sources/>
- Falco `collector` plugin: <https://github.com/falcosecurity/plugins/tree/main/plugins/collector>
- Falco `json` plugin: <https://github.com/falcosecurity/plugins/tree/main/plugins/json>
- Tracee `tracee-rules` JSON input: <https://github.com/aquasecurity/tracee/tree/v0.24.1/cmd/tracee-rules>
- Tracee built-in signatures: <https://aquasecurity.github.io/tracee/v0.24.1/docs/events/builtin/security-events/>
- SigmaHQ Linux rules (`r2026-07-01`): <https://github.com/SigmaHQ/sigma/tree/r2026-07-01/rules/linux>
- RSigma: <https://github.com/timescale/rsigma>
- RSigma streaming daemon: <https://rsigma.io/guide/streaming-detection/>
- RSigma processing pipelines: <https://rsigma.io/guide/processing-pipelines/>
- RSigma systemd deployment: <https://rsigma.io/deployment/systemd/>
- RSigma security advisories: <https://github.com/timescale/rsigma/security/advisories>
- Repository context: `docs/design/perimeter-software-tracking.md`,
  `docs/operations/disaster-recovery.md`, and
  `todo/security/user-data-retention-lifecycle.md`.
