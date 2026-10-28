# RSigma / SigmaHQ spike

This is the offline, reproducible part of the IDS rule-layer spike described in
[`docs/design/intrusion-detection.md`](../../docs/design/intrusion-detection.md).
It answers the narrow question the current forwarder cannot: can Tetragon
`process_exec` events be normalized into Sigma fields and evaluated by an
off-the-shelf Sigma engine without adding a second kernel sensor?

It is not integrated into `ids-forwarder` yet and it does not touch the
cluster.

## What is here

- `../../sigma/normalize.go` — pure Go normalization from Tetragon JSON to the
  flat Sysmon-like fields the SigmaHQ Linux rules use (`Image`,
  `CommandLine`, `ParentImage`, `ParentCommandLine`, `User`, `ContainerName`,
  `EventType`, destination address/port).
- `../../cmd/ids-sigma-bridge` — tiny stdin/file to NDJSON harness around that
  normalizer.
- `rules/` — eight SigmaHQ Linux `process_creation` rules and five
  `network_connection` rules from `r2026-07-01`, vendored under DRL 1.1
  (`rules/LICENSE`), plus `zz-local-suppressions.yml` for the root interactive
  shell tuning found in the replay.
- `pipeline.yml` — RSigma pipeline that adds a per-category `EventType` guard
  to the Linux `process_creation` and `network_connection` rules so they
  cannot fire on the wrong event type.
- `samples/tetragon.jsonl` — synthetic Tetragon events: eight malicious
  behaviours and six benign controls.
- `samples/expected.json` — expected rule per PID, including empty lists for
  the benign controls.
- `run.sh` — builds the bridge, normalizes the samples, runs RSigma, and
  fails if the detection set differs from `expected.json`.
- `audit-cves.sh` — queries OSV for the crates actually linked into the Linux
  RSigma release binary and fails if any advisory matches.

## Run it

RSigma is built from source at pinned `v0.25.0` (commit
`457aaf93253211c6386213e7dd7bcecfcf160f3a`):

```bash
git clone --depth 1 --branch v0.25.0 \
  https://github.com/timescale/rsigma.git ext/rsigma
cd ext/rsigma
cargo build --release --locked
cd ../..
./ids-forwarder/sigma-spike/run.sh
```

`ext/` is gitignored scratch. Set `RSIGMA_BIN` to use a binary built elsewhere.
The script exits 2 with build instructions if RSigma is missing.

## Result

`run.sh` currently prints:

```
ok: 8 detections across 14 events, no benign matches
```

The eight malicious events are the webshell parent/child, netcat reverse
shell, base64 pipe to shell, curl/wget download-and-execute, log clearing,
crontab enumeration, apt pre-invoke shell, and interactive bash. The benign
controls are ordinary apt, nginx worker, `rm` under `/tmp`, HTTPS curl, a
container health probe, and a TCP connect event; none matched. The sample
interactive shell runs as a non-root user, so the local root-session filter
does not hide it.

The daemon shape was also exercised locally: RSigma listened on a Unix socket,
accepted NDJSON from the bridge, wrote detections to a file sink, persisted
state to SQLite, and exposed a loopback management API. It produced the same
eight detections.

## Full SigmaHQ set is noisy

Running the same 14 events against all 122 `process_creation` rules at
`r2026-07-01` produced 15 detections, including two benign hits:

- `File Deletion` on `rm -f /tmp/scratch`
- `Curl Usage on Linux` on a plain HTTPS request

It also duplicated two malicious events with generic rules. The full set is
useful as a rule source, but the pilot should start with a curated,
behaviour-focused subset and add rules only after checking them against the
benign baseline.

## Performance

Measured locally with `rsigma engine eval` on 10,000 normalized events:

| Rules | Elapsed | Throughput | Max RSS |
|---|---:|---:|---:|
| 13 curated (8 process + 5 network) | 0.078 s | ~129k events/s | 16.6 MB |
| 122 full process set | 0.284 s | ~35k events/s | 19.5 MB |

Rule loading was under 0.05 s in both runs. These are userspace costs only;
Tetragon's own sensor cost is unchanged.

## Production replay

Two recent Tetragon windows were copied to local `./tmp` directories with mode
0600, replayed, and deleted; no raw production events entered the repo.

- Storage leader, ~27 minutes: 37,956 `process_exec`, 13,758 normalized
  `tcp_connect`.
- GPU node, ~2 hours of a rotation: 12,486 `process_exec`, 150 normalized
  `tcp_connect`.

Results:

- **13 curated rules plus local filter:** 0 detections in both windows.
  Synthetic positive controls appended to the real streams produced exactly
  one webshell detection and one reverse-shell detection, so the process and
  network paths work.
- **Root interactive shell noise:** before the filter, the storage window had
  1 and the GPU window had 2 `Bash Interactive Shell` hits. All were root
  with no parent binary, the normal cluster-exec/operator shape. The local
  filter suppresses that shape and leaves non-root interactive shells
  detectable.
- **5 network_connection rules:** initially 199 `Linux Reverse Shell
  Indicator` detections on the storage window, all on `process_exec` events
  with no `DestinationIp`. The rules were not guarded by event type, so their
  missing destination field matched. Adding the
  `EventType: network_connection` guard reduced this to 0. Every logsource
  category needs its own guard; the evaluator does not enforce
  `logsource.category`.
- **122 full process_creation rules:** 7,019 detections in the storage window
  and 1,498 on the GPU node, dominated by routine cluster activity:
  - storage: 3,531 network discovery (`ip`, `iptables`), 1,361 system
    information (`hostname`), 965 `curl`, 877 directory discovery
    (`findmnt`), 189 sensitive-file changes
  - GPU: 990 sensitive-file changes, 332 network discovery, 60 scripts in
    suspicious directories, 31 system information, 29 file deletions
- Runtime: 0.12 s / 16.8 MB max RSS for the curated set on the storage window,
  1.17 s / 18.9 MB for the full set.

The curated set is quiet on these two windows, but two windows are not a
baseline. The next gate is a two-week run on a BTF-capable host, not more
offline replay.

## CVE / advisory check (2026-10-28)

- **SigmaHQ rules:** no published security advisories. Rules are data, not an
  executable parser or engine.
- **pySigma tooling (not used by RSigma):** five advisories, including a
  Jinja-sandbox escape and parser DoS issues, fixed in `2.0.0`/`2.0.1`. If we
  use `sigma-cli` for rule conversion or validation, pin `pySigma >= 2.0.1`.
- **RSigma project:** five GitHub advisories, all fixed by `v0.25.0` (the OTLP
  gzip body limit, enricher/webhook field escaping, and three MCP issues). The
  pilot disables OTLP, MCP, enrichers, and webhooks anyway.
- **RSigma dependencies:** the upstream `v0.25.0` lockfile resolves four
  vulnerable crates in the Linux release graph:
  - `ctutils 0.4.2` — `RUSTSEC-2026-0385`, non-constant-time mask generation.
  - `rustls-webpki 0.103.15` — `CVE-2026-111943`, distrusted emSign
    intermediate acceptance.
  - `webpki-roots 1.0.9` — `CVE-2026-111943`, bundled distrusted root.
  - `tokio 1.53.2` — `RUSTSEC-2026-0386`, `AsyncFd` I/O-safety issue.
- The lockfile also contains `block-buffer 0.10.4` (GHSA-qwgh-2vcv-g2f7),
  `webpki-root-certs 1.0.9`, and unmaintained `encoding 0.2.33`, but those are
  not in the Linux release dependency graph.

The TLS/root-store issues are unreachable in the pilot shape because the
daemon has no outbound TLS: input is a unix socket, output is a file, and the
management API is loopback HTTP. RSigma does not call `AsyncFd` directly. The
side-channel issue is a dependency weakness, not a demonstrated RSigma attack.
Patch anyway before any deployment.

The following lockfile updates build cleanly and reduce the release graph to
zero OSV advisories (304 packages checked):

```bash
cd ext/rsigma
cargo update -p ctutils --precise 0.4.4
cargo update -p rustls-webpki --precise 0.103.16
cargo update -p tokio --precise 1.53.3
cargo update -p webpki-roots@1.0.9 --precise 1.0.10
cargo build --release --locked
```

Then run:

```bash
./ids-forwarder/sigma-spike/audit-cves.sh
# packages=304 vulnerable=0
```

`audit-cves.sh` queries OSV for the packages actually linked into the Linux
`rsigma` target, not every stale or dev-only entry in `Cargo.lock`. Set
`CARGO`/`CARGO_HOME` if the local Rust toolchain needs them.

## Dev-cluster blocker

The Incus dev nodes cannot run Tetragon: they have no
`/sys/kernel/btf/vmlinux` and `kernel.unprivileged_bpf_disabled=2`. Only the
userspace RSigma side can run there. A real end-to-end event test needs a VM
or a physical host with BTF and BPF privileges.

## Next step

If this direction is accepted, the smallest production-shaped change is:

1. Add a `-sigma-socket` output to `ids-forwarder` using the same normalizer,
   with a bounded queue and sent/error/dropped counters.
2. Start RSigma as a hardened `DynamicUser` systemd unit with a Unix-socket
   input, loopback API, file sink, and SQLite state.
3. Have the forwarder tail the RSigma detection sink and keep owning the
   Alertmanager spool and redaction path.

Do not replace the existing Go rules until the pilot has run against a
BTF-capable host baseline for two weeks.
