# vLLM Upgrade

Notes for moving the pinned vLLM version forward. vLLM is installed on
Nvidia GPU nodes as a `uv` tool (prebuilt CUDA wheels, run as the `dev`
user) by `admin/install-vllm.yml`. The version is pinned in two places
that must be kept in sync:

- `config/ansible/admin/install-vllm.yml` — the `uv tool install` range
  (e.g. `vllm>=0.20,<0.21`)
- `config/ansible/admin/install-incus.yml` — `vllm_version` for the
  GPU-passthrough VM image

Because we install prebuilt wheels and run vLLM as a plain OpenAI-compatible
server (no source build, no use of vLLM internals), most upgrades are
mechanically low-risk on our hardware. The recurring exception is the
`transformers` major-version boundary — see below.

## Procedure

1. Check the current pin and the latest release:

   ```bash
   grep -n "vllm" config/ansible/admin/install-vllm.yml config/ansible/admin/install-incus.yml
   curl -s https://pypi.org/pypi/vllm/json | jq -r '.info.version'
   ```

2. Read the release notes for every version between the current pin and
   the target. Pull the raw notes rather than a summary (auto-summaries
   have misreported both version numbers and feature names):

   ```bash
   for v in 0.21.0 0.22.0 0.23.0; do
     echo "### v$v"
     curl -s "https://api.github.com/repos/vllm-project/vllm/releases/tags/v$v" | jq -r '.body'
   done
   ```

3. Focus the review on: breaking changes, removed/deprecated symbols,
   the required `transformers` version, and Blackwell (SM12x) kernel
   changes. Cross-check any deprecation against the models actually
   served (etcd `/cluster/config/inference/models/`).

4. Bump the pin in **both** files, sync, and run the playbook against a
   single GPU node first:

   ```bash
   ./run-playbook.sh admin/install-vllm.yml --limit <one-gpu-node>
   ```

5. Smoke-test a served model end to end through the gateway before
   rolling to the rest of the fleet.

## What to watch for

- **`transformers` major bump.** The most likely source of breakage on
  upgrade. A new vLLM may require a newer `transformers` major version;
  some models depend on configs that vLLM vendors only past a specific
  `transformers` release. Validate against the models in use, not in the
  abstract.
- **Removed deprecated symbols / CLI args.** Safe for a plain server
  deployment, but bites anything importing vLLM internals or passing
  arguments that were folded into newer flags (e.g. backend-selection
  env vars subsumed by `--moe-backend` / `--linear-backend`).
- **Model Runner V2.** vLLM is progressively making MRv2 the default per
  model family. It auto-falls-back to MRv1 for unsupported features, so
  it is low-risk, but it is the largest under-the-hood change — watch for
  throughput or correctness regressions on the served models.
- **Source-build-only requirements.** Items like a new C++ standard for
  the compiler affect source builds only; they do not apply to our wheel
  install, but would matter if we ever build from source.
- **Blackwell kernels are upside.** New FlashInfer/CUTLASS FP8 and
  NVFP4/MXFP4 paths and SM12x optimizations land most releases and are
  generally pure performance wins on RTX PRO 6000 Blackwell GPUs.

## History

- **0.20 → (pending).** Reviewed 0.21.0–0.23.0. Headline upgrade gate is
  the `transformers` v4→v5 requirement introduced in 0.21.0; the rest is
  Blackwell kernel/perf improvements, broad NVFP4/MXFP4 quantization
  maturation, progressive Model Runner V2 defaulting, an Anthropic
  Messages API endpoint, and large amounts of new model support.
</content>
</invoke>
