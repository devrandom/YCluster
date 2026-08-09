# Dependency Hygiene Across the Whole Cluster

## TL;DR

- Extend the `local-ai-proxy` pattern (read-only audit vs. write-prone re-pin as
  **separate** flows, `go.sum` tracked so what you scan is what ships) to every
  dependency surface the cluster builds and runs.
- Introduce one aggregate gate, `just deps-check`, that runs reachability-aware
  audits across **Go**, **Python**, and **pinned third-party source builds**
  (`build-*.yml` git-SHA pins under `/opt/src`) **without mutating any pin**.
- Keep the write-prone `deps-tidy-*` recipes for actual bumps, each landing as
  its own reviewed commit. Never fold a pin change into a routine audit.
- Deliverables in this proposal: a per-surface inventory, a per-ecosystem
  scanner and cadence, and a rollout plan. No dependency gets auto-upgraded;
  every bump stays human-reviewed.

## Problem

`local-ai-proxy` is the only surface with any dependency hygiene today — the
`dependency-hygiene` skill + `just.d/deps.just` recipes. An audit run just now
found (and we fixed) 8 reachable CVEs there: a Go toolchain bump to `1.25.12`
and `grpc`/`x/net` bumps. That single hardened module sits at the edge of a
cluster that builds and ships **many** other codebases with pinned dependencies
and no equivalent gate:

- a Python dev/control-plane environment (`pyproject.toml` + `uv.lock`,
  Ansible + collections + `etcd3` inventory plugin + ycluster CLI deps)
- the ycluster CLI runtime package (`config/ansible/admin/files/ycluster/`)
- an MCP server (`mcp/cluster-exec`)
- third-party source trees pinned to git SHAs and built via
  `config/ansible/<area>/build-*.yml` into `/opt/src` (podman, frp,
  zulip, open-webui)
- tooling installed as `uv tool` (serena-agent, ty, mcp-fstools)

Today there is no single place that answers "is everything we run up to date
and free of known, reachable vulnerabilities?" This doc designs that place.

## Scope & surface inventory

"All the code we use" is not one tree. `ext/` is gitignored working scratch;
the shipped third-party pins live in the `build-*.yml` playbooks. So the design
is organized around the **shipped pin**, not the checkout directory.

| # | Surface | Where pinned | Ecosystem | Audit today |
|---|---------|--------------|-----------|-------------|
| S1 | `local-ai-proxy` | `local-ai-proxy/go.mod` + `go.sum` | Go | ✅ govulncheck (`just check`) |
| S2 | Python dev/control plane | root `pyproject.toml` + `uv.lock` | Python (uv) | ❌ |
| S3 | ycluster CLI runtime | `config/ansible/admin/files/ycluster/requirements.txt` (+ `setup.py`) | Python (pip) | ❌ |
| S4 | MCP server | `mcp/cluster-exec/pyproject.toml` + `uv.lock` | Python (uv) | ❌ |
| S5 | Ansible collections | pinned inside Ansible version (S2) | Python | ❌ |
| S6 | podman | `admin/build-podman.yml` `podman_repo_sha` | source (Go/Rust) | ❌ |
| S7 | frp | `app/build-frp.yml` `frp_repo_sha` | source (Go) | ❌ |
| S8 | rathole | being **ripped out** — not a long-term surface | source (Rust) | ❌ |
| S9 | zulip | `app/build-zulip.yml` `zulip_version: 12.1-0` | source tree | ❌ |
| S10 | open-webui + plugins | `app/build-open-webui.yml` `webui_version` + `plugins_repo` | source (Python/Node) | ❌ |
| S11 | vLLM | `admin/install-vllm.yml` + `install-incus.yml` (`vllm>=0.20,<0.21`, dual-pinned) | PyPI wheels (documented exception) | ❌ |
| S12 | OS packages + snaps | security patches via **unattended-upgrades**; leader-election services held (`dpkg --set-selections`); microceph held (`snap refresh --hold`) | system | partial (`packages.md`) |
| S13 | dev tooling | `uv tool install` (serena, ty, mcp-fstools) | Python | ❌ (dev-only) |

**Noteable item:** S8 (rathole) is slated for removal, so it is **not**
pinned and not part of the dependency gate. It is excluded from scope until its
removal lands — tracking it today would mean putting effort into hardening a
component that is going away. Once removal lands, drop it from this table.

## Design

### 1. One principle: audit is read-only, re-pin is write-prone and separate

Reuse the exact contract established for `local-ai-proxy`:

- **Audit** (`just deps-check`) reads locks/manifests and reports reachable
  findings. It never rewrites `go.mod`/`go.sum`/`uv.lock`/`requirements.txt`.
- **Re-pin** (`just deps-tidy-*`) writes pins and belongs in its own reviewed
  commit, keeping lockfiles tracked so the scanned set == the shipped set.

`go.sum`/`uv.lock` staying git-tracked is what makes "what you scanned is what
you ship" true; the audit recipes should verify the lock is tracked and matches
the cache (like `just deps-verify`).

### 2. Aggregate gate

```text
just deps-check
 ├─ S1  govulncheck ./...        (local-ai-proxy; reachability-aware)
 ├─ S2  uv lock --check + osv-scanner  (root uv.lock)
 ├─ S3  pip-audit / osv on ycluster requirements
 ├─ S4  uv lock --check + osv-scanner  (mcp/cluster-exec uv.lock)
 ├─ S5  covered by S2 ansible pin
 ├─ S6–S10 (excl. S8)  osv-scanner on each pinned build tree's lockfiles +
 │          freshness check (pinned SHA vs upstream default branch)
 ├─ S11 checked against PyPI advisory feed (sanity: latest in range)
 └─ S12 (informational; see packages.md) — not part of the green gate
```

Exit policy: **non-zero if any reachable finding is present**
(`govulncheck` exit 3 semantics extended to Python). Unreachable findings
(imported/required with no trace) are noted and don't block — the same
threshold the `local-ai-proxy` skill already uses.

S12 is informational here by design: the OS is already kept patched by
**unattended-upgrades**, so the code gate does not need to own OS patching.
The doc only tracks it so the risk picture is complete — `deps-check` reports
held-package staleness (`HeldPackagesStale`) as context, never as a blocker,
and actual upgrades stay behind the existing `packages.md` playbooks
(`upgrade-held-packages.yml`, `upgrade-microceph.yml`).

### 3. Per-ecosystem tools

| Ecosystem | Scanner (audit) | Re-pin entry | Notes |
|---|---|---|---|
| Go | `govulncheck ./...` | `go get` + `deps-tidy` | reachability-aware; stdlib fixed by toolchain bump |
| Python (uv) | `osv-scanner` on `uv.lock` + `uv lock --check` | `uv lock` / `uv add` bump | osv scans resolved pins; `uv lock --check` proves lock == pyproject |
| Python (pip, S3) | `pip-audit` on requirements | edit `requirements.txt` | S3 mirrors apt runtime deps; keep in step |
| Pinned source trees (S6, S7, S9, S10 — excl. S8) | `osv-scanner` on the tree's lockfiles at the pinned SHA; `git ls-remote` freshness | bump SHA/version in `build-*.yml`, rebuild | we don't own upstream deps; we pin the tree, so hygiene = pin freshness + known-vuln at that rev |
| system | `apt list --upgradable`, `snap refresh --list` | `packages.md` playbooks | held packages are already gated by `HeldPackagesStale` alert |

`osv-scanner` (OSS, from `google/osv-scanner`) is the only new tool import. It
is Go, so it can be installed via `go install` — consistent with the "no
prebuilt binaries; source builds" rule. Everything else reuses tools already
present (`govulncheck`, `uv`, `pip-audit`).

### 4. Directory layout / just recipes

Keep the pattern in `just.d/deps.just` but split per surface so a failure names
the surface:

```text
deps.just
  check           # aggregate read-only gate (all S1–S11)
  deps-verify     # locks tracked + match cache (all surfaces)
  vulncheck       # S1 govulncheck  (existing)
  deps-vuln-py    # S2/S3/S4 osv + pip-audit + uv lock --check
  deps-vuln-src   # S6, S7, S9, S10 (excl. S8) osv-scanner at each pinned SHA + freshness
  deps-tidy-go    # go mod tidy (S1)
  deps-tidy-py    # uv lock (S2, S4)
  deps-tidy-src   # emit proposed new SHA/bump for build-*.yml (no auto-write)
  deps-baseline   # regenerate the accepted-findings ledger (see §6)
```

Each `deps-tidy-*` writes pins and is documented as "must land as its own
commit" — mirror the `local-ai-proxy` skill note verbatim.

### 5. Cadence

- **Pre-ship:** `just deps-check` runs before any release/tag and ideally in
  CI on `main`. This is the gate.
- **Scheduled:** a monthly `just deps-check` catch-all, plus re-pin of stale
  SHA pins when their upstream has security/feature releases worth taking.
- **Event-driven:** when a `build-*.yml` SHA is bumped (S6, S7, S9, S10) or a dependency
  version is added/moved, the re-pin + re-audit happen in the same reviewed
  change (like `local-ai-proxy`).

### 6. Findings ledger

The repo already keeps vulnerability artifacts per component
(`local-ai-proxy/VULN-FINDINGS.md`, `TRIAGE.md`). Generalize:

- A gitignored scratch report `tmp/deps-check.txt` for the full machine-readable
  output.
- A small, version-controlled **accepted-findings ledger** for consciously
  accepted (unreachable / low-severity / Windows-only / internal-only) items,
  so `deps-check` can emit them without failing and reviewers can see why.
  This is the "reachable = fixed or consciously accepted" rule made auditable.

### 7. Policy alignment

- **No prebuilt binaries:** `osv-scanner` installed via `go install`;
  nothing else gets downloaded as a binary. Source builds stay pinned to a
  git SHA under `/opt/src` (S8 rathole is excluded — it's being ripped out).
- **No auto-upgrade:** every `deps-tidy-*` output is a separate reviewed change.
- **Live/prod safety:** audits and re-pins are static; they never touch running
  nodes. OS/held-package upgrades remain behind the existing `packages.md`
  playbooks + rolling procedures.
- **No commits unless asked:** `deps-check` and `deps-tidy-*` produce working
  tree changes only; committing stays an explicit human action.

## Rollout plan

Per-surface plan. Order matters: dependency/sequencing described in each row,
independent items can go in parallel. "Exit criteria" is the definition of done
for that surface's gate.

| # | Surface | Audit now | Plan | Exit criteria |
|---|---------|-----------|------|---------------|
| S1 | local-ai-proxy (Go) | ✅ `just check` | Already wired; extend only to ensure it's included in the aggregate `deps-check`. | `govulncheck ./...` exit 0, `go.sum` tracked |
| S2 | Python dev/control plane (root uv.lock) | ❌ | Add `uv lock --check` + `osv-scanner` to `deps-vuln-py`; baseline ledger. | clean osv on `uv.lock` + `uv lock --check` passes |
| S3 | ycluster CLI runtime (pip) | ❌ | Decide lock vs loose (see Open Q1). Default: keep loose, gate via `pip-audit` against `requirements.txt`. | `pip-audit` clean; manually re-verified against apt runtime deps |
| S4 | MCP server (uv.lock) | ❌ | Same as S2 (`uv lock --check` + osv). | clean osv + lock-check |
| S5 | Ansible collections | ❌ | Covered by S2's ansible pin; verify a collection `requirements.yml` records what's needed so it's not implicit. | collection set reproducible from S2 |
| S6 | podman | ❌ | osv on pinned-SHA tree locks + `git ls-remote` freshness (see Open Q2). | pinned SHA in policy, no reachable finding at that rev |
| S7 | frp | ❌ | same as S6. | same |
| S8 | rathole | ⚠️ `main` | **Tracked separately as removal** (rip it out; excludes rathole config/CLI/install YAML). No dependency hygiene work here — it's going away. | removed from repo + this table |
| S9 | zulip | ❌ | freshness (tag vs. latest) + osv at pinned tree. | pinned tag clean; bump reviewed |
| S10 | open-webui + plugins | ❌ | freshness + osv; plugins repo SHA pinned (matches repo rule). | pinned SHA/tag, clean at that rev |
| S11 | vLLM | ❌ | sanity-check latest in pinned range vs PyPI advisory feed; keep the two files in sync (existing `vllm-upgrade.md` workflow). | pins in `install-vllm.yml` + `install-incus.yml` in sync, no known-reachable advisory |
| S12 | OS packages + snaps | partial | Keep informational; rely on unattended-upgrades; report `HeldPackagesStale` as context. | no code-gate blocker; held-package upgrades via `packages.md` |
| S13 | dev tooling (uv tools) | ❌ | Monitor but exclude from green gate (dev-only); re-pin on `setup-tools` re-run. | not a blocker; documented |

Phase 1 — **unblock & wire** (parallel): S8 removal is tracked separately (not
here); add osv-scanner tooling +
aggregate `deps-check` recipes; verify S1 in the aggregate.
Phase 2 — **baseline** (parallel): run `deps-check`, capture findings into the
accepted-findings ledger for S2, S3, S4, S6, S7, S9–S11.
Phase 3 — **close gaps** (each as its own reviewed commit): fix reachable CVEs
found in Phase 2, re-audit per surface.
Phase 4 — **enforce + runbook**: land `deps-check` in CI/pre-ship; write the ops
runbook extending `docs/operations/packages.md`.

## Out of scope / explicitly deferred

- **Containers** (ComfyUI pods, MinerU, docker-zulip images): separate image
  scanning concern; the repo rule is "no prebuilt binaries", so in-cluster
  images are source-built — a separate `trivy`-style pass is a future effort,
  not part of this gate.
- **Model weights / HF artifacts:** not code dependencies.
- **Auto-merge dependency bots (Renovate/Dependabot):** conflicts with the
  no-auto-upgrade, human-review policy.
- **Runtime attestation / SBOM at boot:** worth a later design; today the lock
  + tracked `go.sum`/`uv.lock` is the reproducibility boundary.

## Open questions

1. Should ycluster CLI (S3) move to a pinned `uv.lock` like S2/S4, or keep the
   apt-mirroring `requirements.txt` (which intentionally stays loose to match
   the `python3-<pkg>` apt packages)? Recommendation: keep S3 loose and gate it
   via `pip-audit` unless a locked runtime is explicitly wanted.
2. How often should the S6/S7/S9/S10 SHA freshness be checked vs. triggered by upstream
   advisories? Default: monthly `deps-check` plus advisory-driven bump.
3. Who owns the accepted-findings ledger per component? Default: the component
   owner (same "top committer" owner convention used by `TRIAGE.md`).
