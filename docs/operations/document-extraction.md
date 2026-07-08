# Document Extraction — Ops

MinerU 3.4.x runs on the macOS inference nodes (m*, hybrid-engine via
`mineru[all]`) and on Linux/Nvidia nodes (nv*, containerised with
`mineru[all]` + vLLM), fronted by `local-ai-proxy` and the `inference.xc`
nginx vhost.

For the **user-facing API surface** (endpoints, form fields, examples,
output format), see [`docs/usage/document-extraction.md`](../usage/document-extraction.md).
This page covers deployment, architecture, and troubleshooting only.

See [`docs/design/mineru.md`](../design/mineru.md) for the underlying
technology choice and MLX-on-Mac rationale, and
[`docs/design/mineru-podman.md`](../design/mineru-podman.md) for the
Nvidia-side container refactor.

## Architecture

```
client → inference.xc/v1/file_parse   (nginx, auth_request)
       → local-ai-proxy :4001         (multipart model-field routing,
                                      /v1/file_parse → /file_parse rewrite)
       → <miner-host>:8000/file_parse (mineru-api FastAPI)
         ├─ macOS (m*): MLX on M3 Ultra, hybrid-engine
         │  (VLM MinerU2.5-Pro-2605-1.2B + pipeline backend),
         │  uv venv, LaunchDaemon
         └─ Linux (nv*): containerised podman stack on RTX PRO 6000
            Blackwell — mineru[all] + vLLM in the image, three
            backends runtime-switchable via the `backend` form
            field, cache bind-mounted from host.
```

- **Service**: `mineru-api` — uvicorn/FastAPI on port 8000. Two
  concurrency regimes:
  - **macOS**: `MINERU_API_MAX_CONCURRENT_REQUESTS=1` (MinerU
    hard-clamps to 1 on macOS regardless of bind — see
    `mineru/cli/fast_api.py:is_mac_environment()` — to keep MLX
    memory pressure predictable on unified memory). The bind-0.0.0.0
    "SSRF mitigation" warning that fires at startup is a *separate*
    concern (disables `*-http-client` backends, see
    `mineru/cli/public_http_client_policy.py`); it does not control
    concurrency.
  - **Linux**: full configured concurrency (`MINERU_API_MAX_CONCURRENT_REQUESTS=3`
    by default — no macOS clamp), so multiple requests can be in
    flight on the GPU simultaneously.
- **Models**:
  - **macOS**: VLM `MinerU2.5-Pro-2605-1.2B` (~2.2 GB) + `pipeline`
    weights (~20 GB) cached in `~/mineru-models` (HF cache).
    Pre-loaded on first install via `mineru-models-download`.
  - **Linux (container)**: VLM `MinerU2.5-Pro-2605-1.2B` (~2.2 GB) +
    pipeline (`PDF-Extract-Kit-1.0`, ~2.5 GB) cached in
    `/home/dev/.cache/huggingface` on the host and bind-mounted into
    the container. The image ships `mineru[all]` so all three
    backends (pipeline / vlm / hybrid) are runtime-switchable via
    the `backend` form field; vLLM is auto-selected as the VLM
    engine on Linux (`get_vlm_engine('auto', is_async=True)`),
    giving proper concurrent page processing.
- **Proxy**: `local-ai-proxy` routes multipart requests by parsing the
  `model` form field (same routing key the JSON endpoints use, just
  discovered differently). The `/v1/file_parse → /file_parse` path
  rewrite is added to the existing tokenizer-aliases switch in
  `handler.go` (parallels `/v1/tokenize → /tokenize` for llama.cpp/vLLM).
- **Auth**: nginx `auth_request` to `local-ai-proxy-auth.service` —
  same master key + Open-WebUI api_key surface as the rest of inference.

## Model registration

```bash
# Replace <host> with the MinerU node:
ycluster inference add http://<host>.xc:8000 document-parse
ycluster inference add http://<host>.xc:8000 mineru-parse
```

`document-parse` is the semantic / user-facing name; `mineru-parse` is
the implementation-specific alias. Both appear in
`inference.xc/v1/models` and both can be used as the `model` form field.

## Opting a host in

The playbooks gate on the per-host `mineru_enabled: true` in
`host_vars/<node>.yml`. Two install paths:

- **macOS (m*)**: `macos/setup-mineru.yml`. Hardware: Apple Silicon
  Mac Studio (M2/M3/M4 Max or Ultra). Installs `mineru[all]` (hybrid
  engine — VLM + pipeline) via `uv` into a venv at
  `~/mineru-venv`. LaunchDaemon plist manages the process. *Not in
  scope for the podman refactor* — Apple Silicon stays on MLX + venv
  because that's the best fit for the platform.
- **Linux/Nvidia (nv*)**: `admin/install-mineru.yml`. Hardware: any
  host-visible NVIDIA GPU (skip is automatic on nodes with
  `ansible_local.gpu.nvidia_host_gpu_count == 0`, e.g. nv3 which has
  every card bound to vfio-pci). Containerises `mineru[all]` + vLLM
  via podman — see the "Container deploy" section below.

`mineru_enabled` and `tei_enabled` are independent — m2 has both,
running TEI on `:8080` and MinerU on `:8000`. They share Metal/RAM;
default `MINERU_API_MAX_CONCURRENT_REQUESTS=1` keeps contention bounded.

## Container deploy (Linux/Nvidia)

The first Nvidia host runs the full pipeline: build the image, push
to the registry, warm up the cache, swap the systemd unit. Future
hosts pull the image from the registry and skip the build.

Image is built from `config/ansible/admin/files/mineru/Containerfile`
(Ubuntu 24.04 + system deps + `uv pip install --system
'mineru[all]' torchvision 'vllm>=0.10.1.1,<0.12.0'`). The image
deliberately does **not** bake MinerU model weights — those are
reused at runtime via bind-mount.

```text
Host (Nvidia)
  /home/dev/.cache/huggingface  ──┐   (HF model cache, ~2.2 GB VLM +
  /home/dev/mineru.json          ─┤    ~2.5 GB pipeline; bind-mounted
                                   │    into the container so weights
  mineru-podman.service            │    survive every redeploy)
    └─ podman run ...              │
         ├─ image: registry.xc:5000/mineru:3.4.3-all-vllm
         ├─ -p 8000:8000            │
         ├─ --device nvidia.com/gpu=all  (CDI)
         ├─ --userns=keep-id       │
         ├─ -v /home/dev/.cache    │
         ├─ -v /home/dev/mineru.json
         └─ entrypoint: mineru-api
```

The systemd unit runs `podman run` as root (the user-mode podman
re-exec path is blocked by CLONE_NEWUSER restrictions on this
kernel/buildah combo); `--userns=keep-id` keeps host UIDs 1:1 inside
the container, so bind-mounted `/home/dev/*` (owned by UID 1002) is
writable by the container process. `Delegate=true` lets podman
create user namespaces under systemd.

## Deploying / Updating

```bash
# From a core node:
ssh "$YC_SYNC_HOST" "cd /etc/ansible && \
  ./run-playbook.sh macos/setup-mineru.yml --limit <m-host>"     # macOS
ssh "$YC_SYNC_HOST" "cd /etc/ansible && \
  ./run-playbook.sh admin/install-mineru.yml --limit <nv-host>"  # Linux/Nvidia
```

`install-mineru.yml` is idempotent:

- Build is skipped if the registry already has the tag (HEAD
  request to `/v2/mineru/manifests/<tag>`).
- Push is skipped in the same case.
- `mineru-models-download` warmup is skipped if the VLM model is
  already cached at `/home/dev/.cache/huggingface/hub/models--opendatalab--MinerU2.5-Pro-2605-1.2B`.
- The systemd unit is rewritten if it differs; the service is
  restarted via the `restart mineru-podman service` handler.

First install takes:

- **macOS**: ~30–40 min (uv install is fast; `uv pip install mineru[all]`
  pulls torch + transformers + mlx + paddle/ppocr; model download pulls
  ~22 GB).
- **Linux**: ~10–15 min (cold cache: buildah pulls Ubuntu 24.04 base,
  `uv pip install` mineru[all] + torchvision + vllm, push ~10 GB image
  to registry, warmup downloads ~22 GB of model weights if not cached).

Subsequent runs are ~30 s (everything cached, registry has the tag,
VLM is in the HF cache).

## Three backends (Linux container)

The container ships `mineru[all]` + vLLM, so all three MinerU
backends are runtime-switchable via the `backend` form field. The
proxy is transparent — it forwards `backend` unchanged. Clients
pick at request time:

- `backend=pipeline` — pure OCR/layout (no VLM); for scanned docs
  where VLM hallucinates text. Fastest.
- `backend=vlm-engine` — VLM only. Fast, good for text-heavy docs
  with simple layout. **The proxy's `contrib/inference ocr` CLI
  sends this by default.**
- `backend=hybrid-engine` — VLM + pipeline sidecars. Best quality
  on edge cases (handwriting, dense tables, formulas). Slower than
  vlm-engine (~30 s extra pipeline overhead at `medium` effort).

If `backend` is absent, MinerU falls through to its own default
(`hybrid-engine` — see `mineru/cli/backend_options.py:DEFAULT_BACKEND`).
With `[all]` installed this is safe (slow but works); without
`backend`, callers using `[all]` get hybrid by default, callers
using `[vlm]` get a 409 (`hybrid-transformers requires local
pipeline dependencies`).

## Rollback

If the container fails on a host, the old venv-based `mineru.service`
is left installed (and disabled) for one-command rollback:

```bash
ssh "$YC_SYNC_HOST" "ssh <nv-host> 'systemctl stop mineru-podman && \
  systemctl enable --now mineru && \
  sleep 2 && curl -sS http://127.0.0.1:8000/health'"
```

The old venv is at `/home/dev/mineru-venv` (untouched). The old
unit file at `/etc/systemd/system/mineru.service` is unchanged. The
proxy URL for that host stays valid (same port, same path) — no
registration changes are needed.

## Body-size knobs

200 MiB is the default max upload size, set in **two** places that must
stay in lockstep:

- nginx `client_max_body_size` in
  `config/ansible/app/files/inference-internal.conf`.
- `maxMultipartBodyBytes` in `local-ai-proxy/router.go`.

Large textbooks can exceed 200 MiB. Bump both in lockstep, or chunk
client-side.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `413 Request Entity Too Large` | Either nginx (`client_max_body_size`) or the proxy (`maxMultipartBodyBytes`) is below the request size. |
| `404` from `local-ai-proxy` with `unknown model: <name>` | Model not registered. Run `ycluster inference add http://<host>.xc:8000 document-parse`. |
| `503 no healthy backend for model: document-parse` | The MinerU service is down or unreachable; on macOS check `launchctl list \| grep mineru`, on Linux check `systemctl status mineru-podman` and `ycluster inference status` from a storage node. |
| `400 Unsupported file type: <ext>` | MinerU rejects non-PDF/DOCX/PPTX/XLSX/images. Convert client-side or use a different model. |
| `409 hybrid-transformers requires local pipeline dependencies` | Linux host has `[vlm]` install (no pipeline deps). Add `backend=vlm-engine` or `backend=pipeline` explicitly, or rebuild with `[all]`. |
| `MinerU API is listening on 0.0.0.0. Disabling *-http-client backends …` in the log | Informational. The miner-api is disabling attacker-driven outbound HTTP (`server_url` / `backend: *-http-client` fields) because the bind is externally reachable — see `mineru/cli/public_http_client_policy.py`. This is a *separate* SSRF mitigation from the macOS concurrency clamp; it does not control concurrency. To re-enable http-client backends while keeping 0.0.0.0, pass `--allow-public-http-client`; or bind to 127.0.0.1 (still cluster-reachable via the proxy). |
| First request slow (~14 s), subsequent ones fast (~0.7 s) | Cold-start model load; the VLM is preloaded at startup (`--enable-vlm-preload true`) but the first parse still pays hybrid-engine init. |
| Linux container fails with "Failed to find C compiler" | The image needs `gcc` + `python3.12-dev` for triton JIT. The Containerfile installs both — if you see this, your image is missing those layers; rebuild from the current Containerfile. |
| Linux container fails with "RuntimeError: Engine core initialization failed" | vLLM cold-compile of CUDA graphs failed (took ~30 s the first time, now cached). Restart the service and watch the journal for the underlying error. |
| `cannot clone: Permission denied` from podman | The systemd unit must have `Delegate=true` and run podman as root with `--userns=keep-id`. Don't `become_user: dev` from ansible for podman operations. |
| `mineru-models-download` prompts interactively for source + model type | Playbook fix: pipe `auto\nall\n` to stdin (already in `install-mineru.yml`). If running by hand, just press Enter twice. |

## Logs

- **macOS node**: `/opt/homebrew/var/log/mineru.log` (mode 0640, dev:admin).
  Both stdout and stderr from `mineru-api` are captured here by the
  LaunchDaemon (mirrors the TEI log convention).
- **Linux/Nvidia node**: `journalctl -u mineru-podman`. podman's
  default log driver is journald, so all stdout/stderr from the
  container (and the systemd-run wrapper) lands in the systemd
  journal. To grep without journalctl: `journalctl -u mineru-podman
  > file`.
- **storage leader**: nginx `/var/log/nginx/access.log` for the
  incoming `/v1/file_parse` POSTs; `journalctl -u local-ai-proxy`
  for proxy-side routing decisions.

## See also

- [`docs/design/mineru.md`](../design/mineru.md) — technology choice,
  MLX-on-Mac rationale, why not Docker, alternatives considered.
- [`docs/design/mineru-podman.md`](../design/mineru-podman.md) —
  Nvidia container refactor rationale, registry strategy, three
  backends, rollback path.
- [`docs/usage/document-extraction.md`](../usage/document-extraction.md) —
  client-facing API: form fields, output formats, examples.
