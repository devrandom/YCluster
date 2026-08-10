# Accepted Findings — Dependency Hygiene Ledger

Baseline captured **2026-08-09** per `docs/design/dependency-hygiene.md` Phase 2.
This is the version-controlled ledger of dependency findings that are
**consciously accepted** (unreachable, constrained, dev-only, or pending a
reviewed re-pin) so `just deps-check` can report them without gating. Anything
listed here either must not block, or is under a tracked remediation below.

Regenerate the raw machine-readable reports with `just deps-baseline`
(writes gitignored `tmp/osv-s2.json`, `tmp/osv-s4.json`,
`tmp/pip-audit-s3.json`). Keep this file in step when a finding is fixed or
newly added. Reachable = fixed (or consciously accepted here); the design
rules say _reachable findings never block silently_.

Disposition legend:
- **FIXED-now** — bumped in this change; re-audit is clean.
- **ACCEPTED** — remediation not practical/required today; rationale given.
- **TRACKED** — known gap, remediation tracked (separate reviewed change).

---

## S1 — local-ai-proxy (Go)

`govulncheck ./...` → **0 reachable** vulnerabilities. Clean; no ledger entry.

## S2 — Python dev/control plane (root `uv.lock`)

`uv lock --check` passes. 20 findings across 11 packages at baseline;
the bulk were fixed in the Phase 3 pass.

| Package | Version→Fixed | Vuln (CVSS) | Disposition |
|---|---|---|---|
| click | 8.3.1 → 8.4.2 | PYSEC-2026-2132 (7.2) | FIXED-now |
| idna | 3.11 → 3.18 | PYSEC-2026-215 (6.9) | FIXED-now |
| flask | 3.1.2 → 3.1.3 | PYSEC-2026-2151 (4.3) | FIXED-now |
| werkzeug | 3.1.5 → 3.1.8 | PYSEC-2026-2320 (6.3) | FIXED-now |
| urllib3 | 2.6.3 → 2.7.0 | PYSEC-2026-141 (8.2), PYSEC-2026-142 (8.9) | FIXED-now |
| requests | 2.32.5 → 2.34.2 | PYSEC-2026-2275 (5.5) | FIXED-now |
| cryptography | 46.0.3 → 50.0.0 | PYSEC-2026-36 (9.8), 2141 (8.2), 3552 (8.2), 3553 (8.7), 3554 (6.9), 35 (5.3), GHSA-537c (7.5) | FIXED-now |
| black (dev) | 25.12.0 → 26.5.1 | PYSEC-2026-2120 (9.8), 2121 (8.7) | FIXED-now (dev formatter) |
| ansible-core | 2.19.5 / 2.20.1 | PYSEC-2026-3458 / CVE-2026-11332 (7.8) | **TRACKED** — see below |
| protobuf | 3.20.3 | PYSEC-2026-1805 (8.2), 1806 (8.2) | **ACCEPTED** — see below |

### ansible-core (TRACKED)

`ansible>=9.0.0` resolves to the `ansible` meta-package (12.3.0 / 13.2.0 in the
lock) which pins `ansible-core` 2.19.5 / 2.20.1. The fix is only published as
`ansible-core` ≥ 2.19.11rc1 / 2.20.7rc1, i.e. a newer `ansible` point release.
Bumping the control-plane Ansible changes playbook behavior and must go through
normal review/deploy, not the audit gate. **Action:** on the next Ansible
upgrade, land `ansible` + `ansible-core` together as its own reviewed change
and re-run `deps-check`; keep this entry until then.

### protobuf (ACCEPTED — etcd3-constrained)

`etcd3` (the etcd inventory plugin) caps `protobuf<4`; the only fixed versions
are ≥ 4.25.8 (PYSEC-2026-1806) / ≥ 5.29.6 (PYSEC-2026-1805). PyPI's newest
etcd3 is still 0.12.0, so there is no compatible newer protobuf. Reachability
is low here: the pure-Python protobuf DoS paths (recursive-message / `Any`
parsing) apply to parsing untrusted protobuf over etcd's cluster-internal
channel. **Action:** re-drop the `<4` cap and accept protobuf 4/5/6 only if/when
we move off etcd3 or pin a protobuf 4-compatible inventory client; reassess on
each `deps-check`.

## S3 — ycluster CLI runtime (`requirements.txt`)

Loose pip requirements mirroring apt runtime deps. `pip-audit` reports only:

| Package | Version | Vuln (CVSS) | Disposition |
|---|---|---|---|
| protobuf | 3.20.3 | PYSEC-2026-1805 (8.2), 1806 (8.2) | **ACCEPTED** — same etcd3 `<4` constraint as S2 |

Prod runs the apt `python3-*` packages, not this file; the DoS paths need
untrusted protobuf parsing over etcd's internal channel. Reassess with the S2
protobuf item.

## S4 — MCP server (`mcp/cluster-exec/uv.lock`)

| Package | Version→Fixed | Vuln (CVSS) | Disposition |
|---|---|---|---|
| cryptography | 49.0.0 → 50.0.0 | PYSEC-2026-3552 (8.2, via `pyjwt[crypto]`) | FIXED-now |

`uv lock --check` passes and `osv-scanner` is clean after the bump.

## S6 — podman (pinned source build)

Pin `podman_repo_sha` = `07efc23e…`, checkout tag **v5.8.0** — **in sync**, no
drift. osv-scanner does not emit lockfile sources from the tree by default
(Go tree). Baseline: fresh pin; nothing to accept.

## S7 — frp (pinned source build)

`frp_repo_sha` = `7b6e01f04…` (tag **v0.70.0**), **in sync** with the
`ext/frp` checkout — no drift. Freshness gap only (not a CVE finding in our
code); re-pin via `deps-tidy-src` on the next deliberate frp bump.

## S9 — zulip (pinned source build)

`zulip_repo_sha` (docker-zulip 12.1-0 release commit) and `zulip_src_sha`
(upstream zulip/zulip 12.1 release commit) are pinned in `build-zulip.yml`;
the operational image tag stays `zulip_version: 12.1-0`. `ext/docker-zulip`
is checked out at `zulip_repo_sha` via `just deps-src-checkout` → **in sync,
no drift**. Source pins are commit SHAs per AGENTS.md "pin source to a git SHA".

## S10 — open-webui + plugins (pinned source build)

`webui_repo_sha` (open-webui v0.11.0 release commit) and `plugins_repo_sha`
(`746f4b2f…`) are both pinned in `build-open-webui.yml`; `ext/open-webui` and
`ext/open-webui-plugins` are checked out at those pins via
`just deps-src-checkout` → **in sync, no drift**. Both are commit SHA pins per
the "pin source to a git SHA" rule (plugins was an unpinned repo URL before).

## S11 — vLLM (PyPI wheels, documented exception)

Pins in sync: `install-vllm.yml` `vllm>=0.20,<0.21` and `install-incus.yml`
`vllm_version: 0.20.1` (0.20.1 is within range). Cross-checked against current
advisories — CVE-2026-44222 (fix ≥0.20.0), CVE-2026-44223 (fix ≥0.20.0),
CVE-2026-22778 (0.8.3–0.14.0), CVE-2026-22807 (0.10.1–<0.14.0) — **none affect
0.20.x**. Clean; no entry.

## S12 — OS packages + snaps (informational)

Not part of the green gate. Held packages / microceph are patched via
unattended-upgrades and the existing `packages.md` playbooks. Documented in
the design; nothing accepted here.

## S13 — dev tooling (`uv tool install`: serena, ty, mcp-fstools)

Dev-only, excluded from the green gate per design. Re-pinned on `setup-tools`
re-run; not tracked in this ledger.

---

## Review cadence

- Reassess **ansible-core** and **protobuf (S2/S3)** on each `just deps-check`,
  and whenever the blocking upstreams move. SRC pins are re-checked by the
  `deps-vuln-src` drift check on every run.
- `deps-vuln-py` / `deps-vuln-src` report these without failing until the
  accompanied entries stop being accepted (Phase 4).
