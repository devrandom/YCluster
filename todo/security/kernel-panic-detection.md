# Detect kernel panics and make them recoverable

Kernel CVEs like CVE-2026-99288 are availability attacks: the failure mode
is a panic, not data loss. Today a panic is both invisible and permanent —
`kernel.panic` is 0 and `RuntimeWatchdogUSec` is 0 on every Linux host, there
is no `/dev/watchdog`, `/sys/fs/pstore` is mounted but empty and nothing
reads it, and the IDS bridge consumer only parses `vm-bridge-guard` kernel
lines. A crashed node hangs until someone power-cycles it, and the only
signal is `NodeDown`, which looks identical to a PSU failure. Unfixed kernel
CVEs are also too numerous to page on individually (~1,000 on a current
6.8 kernel), so exploitation of one currently produces no attributable
signal at all.

## Plan

1. Page on the exploitation signal rather than the CVE: a small collector
   that reports `Kernel panic` / `Oops:` lines from the journal since boot
   and the presence of `/sys/fs/pstore` entries as textfile metrics, with an
   alert naming the host. This covers every kernel CVE at once.
2. Set `kernel.panic=10` on unattended nodes (pstore keeps the trace), so a
   panic reboots into service instead of hanging. Storage and the frontends
   should stay out until their failover behaviour is reviewed.
3. Alert on *new* high/critical unfixed kernel CVEs — first seen within the
   last 7 days and affecting a running kernel — instead of standing
   per-CVE state. That surfaces the next CVE-2026-99288 without the
   thousand-CVE baseline the kernel index reports.

## Acceptance

- A deliberately triggered panic on a dev node produces one alert naming the
  host, and leaves a pstore record that survives the reboot.
- A panic on a prod node with `kernel.panic=10` reboots and rejoins within
  minutes instead of hanging.
- A newly published high/critical kernel CVE that affects a running kernel
  alerts once within a day, and stops after a reboot onto a fixed kernel.
