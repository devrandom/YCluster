# Exo on macOS Nodes

[exo](https://github.com/exo-explore/exo) is the serving path we use on
macOS nodes (m1+) for distributed MLX inference across multiple Macs.
Tensor-parallel mode with ring or JACCL/RDMA transport over the
Thunderbolt bridge is what makes multi-Mac worth the complexity.

## Why exo (vs. alternatives)

The main question was single-request (batch=1) latency on models that
fit on one Mac. Survey outcome:

- **vllm-mlx** — strong single-box batcher (better than `mlx-lm.server`),
  but no multi-node tensor parallelism as of April 2026. One-box only.
- **mlx-lm / mlx.distributed** — has the primitives for TP, but no
  polished server; you'd write the serving layer yourself.
- **llama.cpp RPC** — pipeline-parallel only, and batching is weak.
  Actively moving away from it.
- **exo** — supports both pipeline and tensor sharding, TP across Macs
  shipped in v1.0.63 (Jan 2026), RDMA over Thunderbolt since
  v1.0.65. Dashboard UI + OpenAI-compatible API out of the box.

So: **replicate vllm-mlx per mac for throughput when the model fits on
one box; use exo when the model needs sharding or you want TP to cut
single-request latency.** Both can coexist; LiteLLM routes to either.

## Install

Dashboard requires an npm build and we refuse to run npm on the macs.
Build-once-on-admin, deploy the artifact.

```bash
# One-time on admin host:
git clone https://github.com/exo-explore/exo.git ext/exo
./ext/build-exo-dashboard.sh   # rootless podman container, produces ext/exo/dashboard/build/

# Deploy to macs (checks out same commit, rsyncs dashboard build, drops launcher):
./ext/deploy-exo.sh            # defaults to EXO_HOSTS="m1.yc m2.yc"

# On each mac (manual for now; LaunchDaemon later):
ssh dev@m1.yc './run-exo.sh'
```

Scripts live in `ext/` in this repo. `run-exo.sh` sets `EXO_OFFLINE=1`
and `EXO_LIBP2P_NAMESPACE=ycluster` so peers only discover each other,
not random nodes on the same network.

Prerequisites per-mac (both root *and* dev user, per our macos.md):

- Xcode + Metal Toolchain installed
- `uv` (Homebrew) — exo uses `uv run exo` to manage its venv
- Model weights present (see below)

## Models

Exo expects HuggingFace safetensors repos from the `mlx-community/*`
org — never GGUF. The model card catalog at
`ext/exo/resources/inference_model_cards/` bundles metadata for
~108 pre-characterized models including all MiniMax M2.1/M2.5/M2.7
variants, Qwen3, Llama, GLM, Gemma, etc. Custom quants need a TOML
card, but anything in the catalog is one `/place_instance` call away.

### Where weights have to live

Exo looks in `~/.exo/models/<org>--<repo>/` — **not** the HF cache.
Each expected file must be present as a flat file or symlink in that
directory.

If you already have the weights in the standard HF cache
(`~/.cache/huggingface/hub/models--<org>--<repo>/snapshots/<sha>/`),
symlink rather than copy:

```bash
EXODIR=~/.exo/models/mlx-community--MiniMax-M2.5-4bit
SNAP=$(echo ~/.cache/huggingface/hub/models--mlx-community--MiniMax-M2.5-4bit/snapshots/*/)
rm -rf "$EXODIR" && mkdir -p "$EXODIR" && cd "$EXODIR"
for f in "$SNAP"*; do ln -s "$f" .; done
```

HF snapshot files are themselves symlinks into `blobs/`, so exo ends up
double-indirected but that's fine. No byte duplication.

Caveat: after symlinking, exo's `downloads` state will still show many
`DownloadPending` entries that never complete — harmless bookkeeping.
The runners load the model from disk regardless and reach
`RunnerReady`. Verify via `/state.runners` (see below) rather than the
download counts.

### Distributing weights mac-to-mac

Both Macs need their own local copy (or local-looking symlinks).
Fastest route is rsync over the TB bridge (not the cluster LAN):

```bash
# from m2:
rsync -aHP --info=progress2 \
    dev@<m1-tb-ip>:~/.cache/huggingface/hub/models--mlx-community--MiniMax-M2.5-4bit/ \
    ~/.cache/huggingface/hub/models--mlx-community--MiniMax-M2.5-4bit/
```

~3-5 Gbps effective for SSH-wrapped rsync over TB — 128 GB model
lands in ~5 min.

## Thunderbolt link

The TB cables are exclusively an exo transport — nothing else in the
cluster depends on them. That gives us freedom to configure them the
way JACCL/RDMA wants rather than the macOS-default bridged shape.

### macOS default: bridge0 (broken for JACCL)

macOS auto-bundles every TB port (`en2`..`en7`) into `bridge0` and
assigns a random link-local `169.254.0.0/16` IP on each boot (APIPA).
That shape works fine for plain IP (ping, rsync, MlxRing/TCP) but
breaks RDMA: `mx.distributed.init(backend="jaccl")` fails with
`Changing queue pair to RTR failed with errno 22` because the Apple
RDMA driver (built Nov 2025, new in macOS 26.x) can't bring up QPs
when `en5` is a bridge member running in PROMISC mode.

### Fix: un-bridge en5 and give it a static IP

Run on each mac (only the active TB port needs this — `ifconfig` shows
which `enN` has `status: active`; usually `en5` on a 2-Mac setup):

```bash
sudo ifconfig bridge0 deletem en5
sudo ifconfig en5 inet 10.0.2.1 netmask 255.255.255.0 up    # m1
# …and on m2:
sudo ifconfig en5 inet 10.0.2.2 netmask 255.255.255.0 up
```

Verify: `ifconfig en5 | grep flags` should no longer show `PROMISC`.
`ping -c 3 10.0.2.2` (from m1) should round-trip in <1 ms.

### Persistence across reboots

Neither the un-bridging nor the static IP survives a reboot. Wire a
LaunchDaemon per mac so it's automatic:

```xml
<!-- /Library/LaunchDaemons/xc.ycluster.tb-rdma.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>xc.ycluster.tb-rdma</string>
  <key>RunAtLoad</key><true/>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string><string>-c</string>
    <string>/sbin/ifconfig bridge0 deletem en5;
            /sbin/ifconfig en5 inet 10.0.2.N netmask 255.255.255.0 up</string>
  </array>
  <key>StandardErrorPath</key><string>/var/log/tb-rdma.log</string>
</dict>
</plist>
```

Replace `10.0.2.N` per mac (m1→`.1`, m2→`.2`). Load with
`sudo launchctl bootstrap system /Library/LaunchDaemons/xc.ycluster.tb-rdma.plist`.

### RDMA prereq

Enable once per mac (persists across reboots):

```bash
sudo rdma_ctl enable
rdma_ctl status      # should print: enabled
```

Earlier builds of macOS 26 required enabling from Recovery Mode, but
the 26.2+ `rdma_ctl` command works from a normal shell. Without RDMA
enabled, exo's JACCL backend silently falls back to TCP with the
same perf profile as MlxRing.

## Cluster operations

All API calls go to `http://<mac>:52415`. Any node can answer; they
gossip via libp2p.

### Is the cluster formed?

```bash
# node IDs — each peer should see itself
for h in m1.yc m2.yc; do
  echo "=== $h ==="
  curl -s http://$h:52415/node_id
done

# full state (big, use jq to narrow):
curl -s http://m1.yc:52415/state | jq '.downloads | keys'
# both peers should appear on both sides
```

Dashboard at `http://<mac>:52415/` renders the same state visually.

### Placing an instance

```bash
# Preview all viable placements (server enumerates; params are hints):
curl -sS "http://m1.yc:52415/instance/previews?model_id=mlx-community/MiniMax-M2.5-4bit&sharding=Tensor&instance_meta=MlxRing&min_nodes=2"

# Actually place:
curl -sS -X POST http://m1.yc:52415/place_instance \
  -H "Content-Type: application/json" \
  -d '{
    "model_id":"mlx-community/MiniMax-M2.5-4bit",
    "sharding":"Tensor",
    "instance_meta":"MlxRing",
    "min_nodes":2
  }'
```

Sharding options:

- `Pipeline` — layer ranges split across nodes; each token walks all
  layers sequentially with a TB hop per stage boundary. Best when the
  model doesn't fit on one node. Doesn't help batch=1 latency.
- `Tensor` — all layers mirrored, weight matrices partitioned. Cuts
  FLOPs-per-node and reduces single-request latency proportionally
  when interconnect can keep up. Requires `supportsTensor: true` in
  the model card (most modern MoE / dense models do).

Instance metadata:

- `MlxRing` — ring collectives over plain TCP. Works anywhere, including
  over the TB bridge without RDMA.
- `MlxJaccl` — JACCL backend with RDMA support. Preferred for TB
  clusters once RDMA is enabled; coordinator runs over cluster LAN,
  data plane over TB.

### Check runner state

```bash
curl -s http://m1.yc:52415/state | jq '.runners'
# RunnerReady on both → cluster serving
# RunnerShuttingDown / RunnerConnected → mid-transition
```

### Tear down an instance

```bash
INST=$(curl -s http://m1.yc:52415/state | jq -r '.instances|keys[0]')
curl -sX DELETE http://m1.yc:52415/instance/$INST
```

## OpenAI-compatible serving

```bash
curl -s http://m1.yc:52415/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model":"mlx-community/MiniMax-M2.5-4bit",
    "messages":[{"role":"user","content":"hi"}],
    "max_tokens":200,
    "stream":true
  }'
```

Plug into LiteLLM by adding `http://m1.yc:52415/v1/` as a backend. Any
mac in the cluster serves the same instance (requests proxy to the
coordinator internally), so a single backend URL is enough.

## Benchmarks

Batch=1 streaming on `mlx-community/MiniMax-M2.5-4bit` (128 GB, 62
layers, 4-bit MoE with thinking mode), `temperature=0`, one warmup +
3 timed runs, `max_tokens=300`. Two 512 GB Apple Silicon Ultras with a
Thunderbolt link between them. Script: `ext/bench-chat.py`.

| Config | TTFT | tok/s | Δ vs single |
|---|---|---|---|
| vllm-mlx single-box on one Ultra | 0.25 s | **51.6** | baseline |
| exo TP across two Ultras, MlxRing (TCP over TB) | 0.30 s | 28.8 | −44% |
| exo TP across two Ultras, MlxJaccl (RDMA over TB) | 0.26 s | **45.9** | **−11%** |

RDMA recovers most of the gap that TCP leaves (≈60% speedup over
ring), but TP still costs ~11% vs single-box on this specific model.

Interpretation: per-layer all-reduce cost > FLOPs saved from halving
matmul width, when the model fits comfortably in one mac's unified
memory and most of the bandwidth ceiling goes unused. Same pattern
you'd see on 2×3090 serving a 7B model. The "near-linear scaling"
claim in exo's blog applies to bandwidth-bound regimes (see below) —
M2.5-4bit isn't one.

### Regime framing (why M2.5 was the wrong test)

Whether TP helps batch=1 depends on which resource is actually bound:

- **Bandwidth-bound**: active-weight bytes/token approach the mac's
  memory bandwidth ceiling. Splitting weights across N macs at TP=N
  lets each mac read its shard in parallel. TP wins.
- **Compute/kernel-launch-bound**: observed tok/s is a small fraction
  of the bandwidth ceiling. TP's bandwidth-aggregation advantage
  doesn't matter; comms cost is pure overhead. TP loses.

M2.5-4bit is the wrong test case for TP-wins-at-batch=1 because it's
firmly in the second regime: only ~10 B active params → ~5 GB/token
active read → bandwidth ceiling on a ~800 GB/s Ultra ≈ 160 tok/s.
We observed 51.6 tok/s single-box (≈32% of peak), so the remaining
gap is kernel/compute, which TP cannot help with. MoE sparsity +
4-bit quant conspire to make the model too light to stress memory.

"Fits on one mac" and "benefits from TP at batch=1" are independent.
M2.5 fits *and* doesn't benefit. Something like Kimi-K2-4bit (~32 B
active → ~16 GB/token, ceiling ≈ 50 tok/s on one Ultra) fits on a
512 GB Ultra with essentially zero KV headroom, and is plausibly
bandwidth-bound — that's the test that would have been more
informative than M2.5.

### When to use exo vs. single-box vllm-mlx

| Goal | Use |
|---|---|
| More throughput on a model that fits on one mac | **replicated vllm-mlx** behind LiteLLM — N×tps from N macs, no comms cost |
| Lower batch=1 latency, small active-weight model (MoE, heavy quant) | vllm-mlx single-box (TP won't help — kernel-bound, see above) |
| Lower batch=1 latency, large active-weight model at the bandwidth ceiling | **exo TP MlxJaccl** — TP lets per-mac reads scale out |
| Run a model that exceeds one mac's unified memory | **exo TP MlxJaccl** — no alternative; required topology |
| Long-context on a model that barely fits one mac (no KV room) | **exo TP MlxJaccl** — splitting weights frees memory for KV |
| Serve multiple models concurrently | vllm-mlx per mac, different models each |

## Open items

Exo is operational but shelved for non-trivial experiments until one
of the below is needed:

- LaunchDaemon to auto-start `run-exo.sh` on boot (paralleling
  `com.ycluster.llama-server.plist`). Until then, run manually.
- Bench a bandwidth-bound model to validate the regime claim above.
  Candidates: Kimi-K2 at 3.6-4 bit (~450-500 GB, ~32 B active),
  MiniMax-M2.7-bf16, or DeepSeek-V3-class. These are models where
  active-weight bytes/token approach a single Ultra's memory ceiling.
- Plan for disk pressure when staging these — TP needs the weights
  locally on every participating node (or symlinked from the HF
  cache per this doc). Model footprint at 3-4 bit is commonly a
  sizeable fraction of an internal SSD, so free-space budgeting is
  worth doing before starting a multi-node download/rsync.
- Measure exo-JACCL concurrent throughput (parallel requests). TP
  may still come out ahead there even on kernel-bound models if
  vllm-mlx's per-mac batcher saturates under load.
- Test speculative decoding inside vllm-mlx as an orthogonal batch=1
  win — likely more leverage than TP on MoE models.
