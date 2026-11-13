# IDS coverage and evidence: the host-root boundary

## Status

Accepted 2026-11-13. This classifies the output of the detection design in
[`intrusion-detection.md`](intrusion-detection.md); it does not restate or
replace it. The implementation tracker is
[`todo/security/intrusion-detection.md`](../../todo/security/intrusion-detection.md).

## Why this exists

The threat model assumes a competent adversary: one who knows the rules and the
suppression table, chooses the path that avoids detection, and treats the
sensor as part of the target. A host-root attacker stops or rewrites the local
sensor before doing anything else, so a sensor running under a compromised root
cannot report what that root does. Post-root host activity is therefore out of
scope, not a coverage gap to be closed.

That changes what the fleet should page on. Under the old model every rule was
implicitly a detection claim, and the paging subset was mostly root-only
signals. Under this model those signals are a record, and the detection claim
moves to the activity that happens while the attacker still lacks host root.
This document says which is which, where the decision is made, and how a new
rule gets classified so the answer does not drift.

## Boundary

- **In scope.** External attackers before they obtain host root; malicious
  tenants and containers, including escape attempts, neighbour traffic and
  lateral connects into the cluster; non-root users and stolen non-root SSH or
  WireGuard keys; privilege escalation from a non-root identity up to the
  point root is obtained; authenticated actions an edge log cannot
  distinguish from legitimate use.
- **Out of scope.** Post-root host activity: root interactive sessions, root
  SSH commands, a stolen root key used on a monitored host, root operator and
  agent credentials, root file writes, kernel module loads, and anything that
  happens after the local sensor is stopped or rewritten. A stolen root key on
  a monitored host therefore pages nowhere until the attacker disables a
  sensor, and the sensor-down alert is the only signal. That is the accepted
  consequence of this model, not an oversight.
- **Guest root is not host root.** uid 0 inside a tenant container or VM has no
  privilege on the monitored host, so every guest-layer event stays in scope
  regardless of uid. Only host-layer root is excluded. A privileged container
  is the exception: it holds host root in practice and can stop the sensor, so
  it is treated as host root. It is visible without guessing: unprivileged
  containers run above the idmap base (uid 1000000 and up), so a container
  event with a host-visible uid below that base is privileged.

## Delivery classes

Three classes, assigned by the forwarder, not by Alertmanager:

- **evidence** — recorded in `detections.jsonl`, never delivered. Post-root
  host activity. Useful for a later incident review, not a control.
- **coverage** — the in-scope signals. Delivered and eligible to page, subject
  to the paging set below.
- **audit** — an operational trail that is neither: Incus API usage, the
  container runtime's fork helper, and network-guard blocks. Delivered silent
  or counted per guest, and exempt from the evidence rule because the trail is
  the point.

## Input contract

The classifier reads a layer, an identity and a session. Each producer supplies
them differently, so the contract is explicit and a missing field has a defined
answer.

| Producer | Layer | Identity | Session |
|---|---|---|---|
| Tetragon rules | process or parent pod/docker metadata, then the cgroup lookup | `uid` from the event process | `origin`/`actor` from the process cgroup, then the parent |
| Sigma | `Layer` from the event, then the `/proc` lookup | `User`, which is `root` or the numeric uid | from `ProcessId`/`StartTime` through the same resolver and the same parent fallback |
| CrowdSec | always host | none, it is log-derived | none |
| Bridge guard | network | none | none |

Evidence needs positive proof of host root. An unknown identity (`uid` on the
Tetragon path, `User` on the Sigma path) never produces evidence; it resolves
to coverage, because a surplus page is cheap while a misclassified event is a
silent gap. The session is different: it is read only by the transition check,
so an unknown session does not change the class of a root event, which stays
evidence. For a privilege-tool event whose session cannot be resolved the
event stays coverage, because a duplicate page is cheaper than a dropped
escalation.

Sigma alerts carry no origin or actor today (`sigma_detect.go` never calls
`applyOrigin`), and no alert carries `uid`; this change adds both, and carries
the audit uid in the normalized event so the escalation fallback survives the
round trip through the rule engine. The namespace snapshot does not travel:
the layer is resolved before the event is sent, and RSigma echoes back what it
received.

## Classification

```text
evidence(a) = rootIdentity(a)          // uid 0, or Sigma User == "root"
           && hostLayerOrPrivilegedContainer(a)
           && !escalationTransition(a)
```

`hostLayerOrPrivilegedContainer` is `layer == "host"`, or `layer ==
"container"` with a host-visible uid below the idmap base. A privileged
container process that has already exited loses its container attribution and
resolves to host with uid 0, which lands in the same class, so the answer does
not depend on `/proc` timing.

`escalationTransition` is narrow on purpose. It is true only for the events
that *perform* the transition: a `process_exec` of `sudo`, `su`, `doas` or
`pkexec`. The invoker is the *parent* process, because the tool itself is
already root when the event is written; when the parent has left `/proc` the
audit uid carried on the event is the next source, and the login session of the
event process is the last. Only that last one needs a live `/proc`, and an
unresolvable invoker leaves the event as evidence rather than paging on a
guess. The source of each verdict is counted in
`ids_forwarder_provenance_total{check="escalation",source=...}`.

The parent is the right identity, not the session: `su - someadmin -c sudo ...`
run inside a root session elevates an unprivileged account, and an operator who
logs in as root and steps down and back up is not crossing a boundary. Reading
the session alone missed the first case and paged on neither. The credential
commit that follows the exec is recorded as evidence, so one sudo pages once.
A *container* layer suppresses the label, and so does an `unverified` layer
when a cgroup actually explained it: a user-scope runtime or a pid-namespace
sandbox outside any runtime root is a tenant, not a host process. An
`unverified` layer that only the uid fallback produced is undecided, and stays
coverage — a duplicate page there is the cheaper mistake.

This scoping is what keeps the model honest. A root shell spawned from an
operator's session is evidence, exactly like any other post-root activity,
because the attacker already has root at that point. What pages is the tool
that crossed the boundary. Treating every root event in a non-root session as
coverage was rejected: it depends on sudo creating a root-owned session through
`pam_systemd`, and it makes paging depend on `/proc` timing for short-lived
processes.

| Rule | Layer and identity | Class |
|---|---|---|
| `process_exec` of `sudo`, `su`, `doas`, `pkexec` from a non-root login session | host, uid 0 | coverage (`escalation_nonroot`) |
| the `privilege-raises` commit that follows that exec | host, uid 0 | evidence (`root_privilege`), not a second page |
| `process_exec`, `exec-root-user-path`, any other root exec | host, uid 0 | evidence (`root_exec`) |
| `process_exec` | host, uid != 0 | coverage (`exec_nonroot`) |
| `tcp-connect`, private destination | host, uid 0 | evidence (`root_connect`) |
| `tcp-connect`, private destination | host, uid != 0 | coverage (`lateral_nonroot`) |
| `tcp-connect`, cluster destination | container, unverified | coverage (`lateral_container`) |
| exec, write, module and privilege rules | container, host-visible uid below the idmap base | evidence (privileged container) |
| `privilege-raises`, any other case | host, uid 0 | evidence (`root_privilege`) |
| `privilege-raises` | container | coverage (`container_other`) |
| `kernel-modules` | host | evidence (`root_module`) |
| `kernel-modules` | container | coverage |
| File writes below `/etc` and unit paths | host, uid 0 | evidence (`root_file_write`) |
| File writes below `/etc` and unit paths | host, uid != 0, or container | coverage |
| Sigma, level medium or above | host, `User=root` | evidence (`root_sigma`) |
| Sigma, level medium or above | host, non-root `User` | coverage (`sigma_nonroot`) |
| Sigma, any level | container | coverage (`sigma_nonroot`) |
| Sigma, level low or informational | host, non-root `User` | coverage, never pages |
| `container-api`, `container-exec` | host, root | audit (exempt) |
| `vm-bridge-*` blocks | network | audit (exempt) |
| CrowdSec scenarios | host, log-derived | coverage, silent until measured |

The privileged-container row overrides the container rows for exec, write,
module and privilege, and appears once rather than repeated against each rule.
Connects stay `lateral_container` and Sigma stays `sigma_nonroot` whatever the
guest uid: the network path is a tenant-boundary signal either way, and the
curated Sigma set is the only thing that sees inside a container.

The exemption for `container-api` and `container-exec` is deliberate: they are
root-admin activity by definition, but they are the only record of who entered
a container and when, so they stay in Alertmanager as an audit stream rather
than disappearing into the evidence file.

## Delivery mechanism

The forwarder sets `page=yes|no` on every `IDSAlert` and Alertmanager routes on
that one label. Rule ids and Sigma levels leave the route table; severity still
selects the receiver. The paging set is one function, so adding or removing a
class is a single code change with no configuration change.

The class is part of the dedup key. `dedupKey` deliberately drops `origin` and
`actor`, so without this a root event would occupy the key that a later
non-root event of the same shape needs, and the coverage alert would vanish
into a dedup hit. Keying on the class also makes the shadow measurement below
safe to run.

Classification runs inside the enqueue path, in this order: record, evidence,
boot grace, dedup, coalescing, rate limit, spool. The position matters in two
ways. An evidence alert must not mark a dedup key, spend a rate slot, or be
counted as boot suppression, because those would each change what a later
coverage event does. And an alert that never reaches the spool must be dropped
before the spool append, so the record and the delivery decision cannot
disagree.

### Shadow measurement

A class that is coverage but not yet enabled is counted where it *would* have
been delivered: after dedup and coalescing, as
`ids_forwarder_page_candidates_total{class=...}`. Nothing is delivered for it.
That gives a true paged-equivalent rate without paging anyone, which is what
the gate below reads.

### Rollout order and the fail-closed signal

Deploy the forwarder to the fleet first, then the Alertmanager route. An old
forwarder emits no `page` label, and `IDSAlert` without it routes to the silent
receiver, so the failure mode of that order is silence rather than a storm. To
make the silence visible, the forwarder exposes
`ids_forwarder_build_info{page_schema="1"}`, and a Prometheus rule warns when a
node reports `ids_component_up{component="ids-forwarder"} == 1` but no
page-schema series for fifteen minutes. Without that check, a host left on the
old binary would drop its escalations without a trace.

## Evidence record

`detections.jsonl` gains `origin`, `actor`, `uid`, `class` and `delivery`.
`class` names the threat-model class above, and `delivery` is `page`, `silent`
or `evidence`, which is what the measurement and a later incident review read.

This file is the only durable copy. A host-root attacker can delete or rewrite
it, exactly as they can stop the sensor; that is the assumption the model
makes. Alertmanager holds a delivered alert for its TTL, ten minutes by
default, and is equally writable by root, so it is a triage surface and not a
forensic store. Shipping classified detections off-host is the only design that
would change this, and it is an open item rather than part of this change.

## Paging set and the measurement gate

Removing root pages leaves the paging set nearly empty. Sigma is the only
in-scope class that pages today, at medium and above; two of the curated rules
are `level: low` and stay silent.

| Class | What it covers | At landing |
|---|---|---|
| `escalation_nonroot` | `sudo`/`su`/`doas`/`pkexec` execs and their root transitions from a non-root login | page |
| `sigma_nonroot` | curated Sigma hits, medium and above, on non-root host processes | page |
| `exec_nonroot` | host shells and allowlisted execs by non-root identities | silent, measure |
| `lateral_nonroot` | private cluster connects from a non-root host process | silent, measure |
| `lateral_container` | container or unverified process connecting to a cluster address | silent, measure |
| `crowdsec` | sshd and nginx scenarios | silent, measure |

A class is enabled when the shadow counter shows at most one page per day
fleet-wide over seven days and every occurrence is explainable from the record.
Seven days and not one, because the shapes that dominate the suppression table
(certificate renewal, package timers, monthly rotation) do not all appear in a
day, and rotation can hide a peak in a short window. Anything above the bar
needs the explainable shape classified as routine first, then a second
measurement. The numbers go in the private signal review, not in this document.

`escalation_nonroot` is the exception that pages at landing without a shadow
measurement. Its rate is unknown, and a transition out of a non-root identity
is the one event this design exists to page, so the first measurement happens
in production with the notification budget as the brake.

Two pipeline details travel with the class. `process_exec` is filtered by
`ids_forwarder_exec_binaries`, which listed `sudo` and `su` only until this
change added `doas` and `pkexec`; without that the class row for those tools is
hollow. And both the shadow counter and the build-info series are in the
textfile allowlist in `ids-metrics-collector.py`, which is the only path to
Prometheus from a loopback endpoint; without that the gate and the stale-schema
check have no series to read.

## Alerting

Coverage alerts are a second, independent paging path. `IDSForwarderGap`
rewrites the alertname so it is not an `IDSAlert` at all, and the sensor
alerts are separate rules. "The paging set is one function" is therefore true
of detections only; the coverage path is deliberately separate and must not be
folded into it.

- `IDSAlert` with `page=yes` routes by severity to the notification receivers.
  Everything else in the class routes to `ids-silent` and stays visible in the
  Alertmanager API and the alert UI.
- Complete loss of a detection component is critical and carries
  `ids_coverage=true`: `TetragonDown`, `IdsForwarderDown`, `RsigmaDown` and
  `CrowdsecDown`. They are now the expected first signal of a host-root
  compromise, so they must not sit in a host-specific stream: the webhook sends
  `ids_coverage=true` to the IDS topic instead of the host topic.
- Degradation stays warning, with the same label: backlog, drops, expiries,
  rate limiting, cursor failures, Sigma tail failures and the gap alert.
- The cluster-wide five-per-severity-per-hour brake is unchanged. It keys on
  `IDSAlert`, so the sensor alerts are outside it and cannot be crowded out by
  a detection storm.

## Open items

- **Off-host evidence.** Nothing ships classified detections off the host, so
  the record lives only on a machine the attacker may already control. Without
  it the evidence class is a decision not to page, not a forensic capability.
- **Broader escalation shapes.** Setuid helpers other than the four named
  tools, and a shell that sudo spawns in the caller's session, are evidence.
  Extending the exception beyond the transition event needs a decision about
  how much of a non-root session's root work stays in scope.
- **Sigma low-level rules.** `Bash Interactive Shell` and `Crontab Enumeration`
  are `level: low`; whether a curated low-level rule should page at all is
  open.
- **Non-root exec from `/var/tmp`.** `exec-root-user-path` is root only, and
  the forwarder cannot stat home directories. A non-root payload staged in the
  world-writable temporary directory is in scope and uncovered.
- **Boot grace and non-root unit activity.** The grace still hides unit and
  unknown-origin `process_exec` for five minutes after boot, which includes
  non-root unit shapes that are now coverage.
- **Integrity hooks.** Rename, link and mmap hooks were ranked as detection
  coverage. Under this model they protect the evidence record, not the
  detection claim, and belong behind the tenant and ingress work.
- **File-integrity instrumentation.** Every host write below `/etc` is root, so
  the FIM rule is now evidence by construction. Whether it is worth
  instrumenting at all is a decision, not a given.
- **Guest internals.** Containers and VMs remain opaque to host sensors; the
  guest boundary is unchanged.

## Review prompt addendum

The suppression blind-spot review prompt in
[`intrusion-detection.md`](intrusion-detection.md) carries this paragraph, so a
review does not spend its findings on the excluded case:

```text
Assume host root is out of scope: an attacker with root on a monitored host
disables or rewrites the sensor, so post-root host activity is evidence, not
coverage. Report only paths that reach the attacker's goal before host root.
Root inside a tenant container or VM is a different identity and is in scope.
Do not report a post-root host path as a blind spot; do report a rule or
document that still claims coverage for one.
```
