# Changelog

Completed work, moved out of `TODO.md`. Newest first. One line per change;
detail lives in commits and code. Items with residual work stay in `TODO.md`.

## 2026-06-26

- **GLM-5.2 defaults to PP=2×TP=4 on the full nvidia checkpoint.** `install-glm52.yml` now points at `nvidia/GLM-5.2-NVFP4` (full 744B; the REAP 504B was retired) and the start script defaults to a PP=2×TP=4 full-box layout — ~2.2× prefill over flat TP=8 on this no-NVLink hardware, validated by the smoke test (A 14/14, B 7/7 to 200k). Adds a `PP` knob and a `DISABLE_MTP` toggle (MTP weights are present but gated off pending validation).
- **GLM-5.2 start script gains tuning overrides.** `MAX_NUM_BATCHED_TOKENS`/`MAX_NUM_SEQS` passthrough and an `EXTRA_VLLM_ARGS` escape hatch (e.g. `--pipeline-parallel-size 2` with `TP=4` for a PP=2×TP=4 layout) — no image rebuild needed.
- **Inference perf bench.** `contrib/inference-bench.py` streams cold prefill (TTFT, prefix-cache-busted) and steady decode tok/s against any gateway model, for before/after backend tuning.
- **podman/CDI now a global GPU-node baseline.** `setup-gpu-podman.yml` moved into `admin.yml`, so Nvidia nodes get rootless-podman + nvidia CDI on a normal `site.yml` run instead of a manual step.
- **MiniMax-M3 start script self-tunes memory to host RAM**, letting it run on RAM-constrained GPU nodes that previously host-OOM'd during warmup (capped cgroup + swap-spill for the warmup peak + throttled compile fan-out); ample-RAM nodes unchanged.

## 2026-06-24

- **MiniMax-M3 tool calling enabled.** M3 moves to the dark-devotion b12x image, which ships upstream's built-in `minimax_m3` reasoning + tool-call parsers (the old image had neither) — closing M3's tool-calling gap. A served-name wrapper keeps the `brandonmusic/minimax-m3-nvfp4` gateway id, and fp8 kv-cache works on this build (the old image needed a bfloat16 workaround). GLM-5.2-504B retired from user-facing use (REAP-pruning factual-knowledge damage).

## 2026-06-23

- **GLM-5.2 on the dark-devotion image.** `0xSero/GLM-5.2-504B` moved to the GLM-specific b12x build (DCP global-topk / topk-scores patch), fixing the >8k-context output corruption the shared M3 image showed on the DSA path; validated to 168k tokens via the inference smoke test.
- **Smoke test handles reasoning-model thinking toggles.** `inference-smoke-test.py` now sends both `enable_thinking` and `thinking_mode`, fixing false long-context failures on GLM/Qwen whose token budget was eaten by an un-suppressed reasoning preamble.

## 2026-06-21

- **GLM-5.2 (REAP-504B) NVFP4 deployed.** `0xSero/GLM-5.2-504B` serves co-resident with M3 on the b12x stack (SM120, TP=4), registered with the gateway, with working tool calling — closing M3's tool-calling gap. Design + incident: `docs/inference/glm-5.2-nvfp4-sm120-design-2026-06.md`.
- **Inference `backends` CLI prefix fix.** `backends disable/enable/ls` now use the disabled-backends etcd prefix instead of the models prefix (disable had been a silent no-op that littered the model store with junk URL-keyed entries), and `models add` rejects URL-shaped or empty model names.

## 2026-06-15

- **Clock-skew health band by node type.** Non-storage nodes now warn at 1s / crit at 10s (was 100ms / 1s); storage nodes keep the strict band since etcd/PostgreSQL need tight sync.

## 2026-06-14

- **Gateway-VIP hardening.** Health check now gauges fitness by live reachability to the frontends instead of one external HTTP GET (which flapped the VIP on any WAN blip); uplink-less nodes (s4) are excluded from holding the VIP.
- **Frontend host firewall.** The rathole and blackbox playbooks now manage a least-privilege ufw on frontend nodes (previously an implicit cloud security group).

## 2026-06-10

### Security & correctness

- **Verified review bugs B1–B8** (`docs/reviews/2026-06-09-codebase-review.md`) fixed and deployed: timing-safe key compare, allocation-CAS check, DHCP check-then-use race, Go-proxy retry scope, SQL parameterization, `wipe-etcd.yml` become fix, download checksums, non-silent inventory `except`.
- **Admin API S1 — privilege drop:** runs as a sudo-free `admin-api` user under waitress with `NoNewPrivileges`/`ProtectSystem=strict`; ceph check uses a read-only `client.admin-api` identity.
- **Admin API S2 — mutations moved to mTLS CLI:** removed unauthenticated mutating POSTs from `app.py`; drain/disable/enable/set-asset are now `ycluster` CLI writes to etcd; CA unified to `/etc/ycluster/ca`.
- **Admin API S3 — route param validation:** `@validated_hostname` on all hostname routes returns 400 before any etcd interpolation.
- **Drain-function auth — superseded** by the S2 mTLS-CLI move (status-page mutation UI deprecated).
- **H4 — `no_log` audit:** added `no_log` to every Ansible task handling a cleartext secret; rathole token read from etcd at task time so inventory dumps can't print it.
- **etcd access hardening — mTLS-only:** core etcd runs in `enforce` mode with no plaintext listeners (`docs/design/etcd-access-hardening.md`); dead RBD CA tree deleted after burn-in.
- **Reduced vault-secret exposure in `ansible-inventory`** (`297d1c0`): vaulted group_vars are play-scoped via `vars_files`, so `--host` dumps decrypt nothing.

### Infrastructure

- **GPU-VM guest driver survives kernel upgrades:** `incus-build-gpu-image.sh.j2` installs `linux-headers-virtual` so DKMS rebuilds the NVIDIA module when unattended-upgrades stages a new kernel.
- **incus VM DNS host-records:** pinned VMs get a permanent DNS record (`sync_dns_records()` in `vm_manager.py`) so name-based SSH doesn't depend on a live DHCP lease.
- **Frontend-node management cleanup:** `/api/hosts` emits `.xc` records for frontend nodes; rathole token and vaulted group_vars no longer ride in inventory.
- **Failed-systemd-units alert:** `SystemdUnitFailed` Prometheus rule plus `monitoring/clear-benign-failed-units.yml` to clear condition-detected benign offenders.
- **"core" definition consistency:** rathole SSH ingress parametric per core node; `core`/`etcd` backed by a static s1–s3 floor plus dynamic union; Tang/Clevis unlock set derived from `groups['etcd']`.
