# Document Extraction — Ops

MinerU 3.4.x runs on the macOS inference nodes (m1+) as a Python
service, fronted by `local-ai-proxy` and the `inference.xc` nginx vhost.

For the **user-facing API surface** (endpoints, form fields, examples,
output format), see [`docs/usage/document-extraction.md`](../usage/document-extraction.md).
This page covers deployment, architecture, and troubleshooting only.

See [`docs/design/mineru.md`](../design/mineru.md) for the underlying
technology choice and MLX-on-Mac rationale.

## Architecture

```
client → inference.xc/v1/file_parse   (nginx, auth_request)
       → local-ai-proxy :4001         (multipart model-field routing,
                                      /v1/file_parse → /file_parse rewrite)
       → <miner-host>:8000/file_parse (mineru-api FastAPI, MLX on M3 Ultra)
         └─ hybrid-engine (VLM MinerU2.5-Pro-2605-1.2B + pipeline backend)
```

- **Service**: `mineru-api` — uvicorn/FastAPI on port 8000. Default
  `MINERU_API_MAX_CONCURRENT_REQUESTS=1` on macOS (MinerU clamps
  concurrency when listening on `0.0.0.0` to mitigate SSRF risk in
  the http-client backends; overridable via env var on a private bind).
- **Models**: VLM `MinerU2.5-Pro-2605-1.2B` (~2.2 GB) + `pipeline`
  weights (~20 GB) cached in `~/mineru-models` (HF cache). Pre-loaded
  on first install via `mineru-models-download`; subsequent boots use
  `MINERU_MODEL_SOURCE=local` to skip the network re-check.
- **Proxy**: `local-ai-proxy` routes multipart requests by parsing the
  `model` form field (same routing key the JSON endpoints use, just
  discovered differently). The `/v1/file_parse → /file_parse` path
  rewrite is added to the existing tokenizer-aliases switch in
  `handler.go` (parallels `/v1/tokenize → /tokenize` for llama.cpp/vLLM).
- **Auth**: nginx `auth_request` to `local-ai-proxy-auth.service` —
  same master key + Open-WebUI api_key surface as the rest of inference.

## Model registration

```bash
# Replace <host> with the MinerU node (currently m2.xc):
ycluster inference add http://<host>.xc:8000 document-parse
ycluster inference add http://<host>.xc:8000 mineru-parse
```

`document-parse` is the semantic / user-facing name; `mineru-parse` is
the implementation-specific alias. Both appear in
`inference.xc/v1/models` and both can be used as the `model` form field.

## Opting a host in

The playbook gates on the per-host `mineru_enabled: true` in
`host_vars/<node>.yml`. Hardware requirement: Apple Silicon Mac
Studio (M2/M3/M4 Max or Ultra). M3 Ultra (512 GB unified memory, 80 GPU
cores) is the current host. NVIDIA is faster (vllm extra) but Docker-only
on Linux; for our deployment, MLX-on-Mac is sufficient.

`mineru_enabled` and `tei_enabled` are independent — m2 has both,
running TEI on `:8080` and MinerU on `:8000`. They share Metal/RAM;
default `MINERU_API_MAX_CONCURRENT_REQUESTS=1` keeps contention bounded.

## Deploying / Updating

```bash
# From a core node:
ssh "$YC_SYNC_HOST" "cd /etc/ansible && ./run-playbook.sh macos/setup-mineru.yml --limit <host>"
```

The playbook is idempotent:

- uv + venv + `uv pip install mineru[all]` are skipped if already present.
- `mineru-models-download` is skipped if the VLM model is already cached.
- The LaunchDaemon plist is rewritten if it differs; the service is
  restarted via the `restart mineru service` handler.

First install takes ~30–40 min (uv install is fast; `uv pip install
mineru[all]` pulls torch + transformers + mlx + paddle/ppocr; model
download pulls ~22 GB). Subsequent runs are ~30 s.

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
| `503 no healthy backend for model: document-parse` | The MinerU service is down or unreachable; check `launchctl list \| grep mineru` on the miner host and `ycluster inference status` from a storage node. |
| `400 Unsupported file type: <ext>` | MinerU rejects non-PDF/DOCX/PPTX/XLSX/images. Convert client-side or use a different model. |
| `MinerU API is listening on 0.0.0.0. Disabling *-http-client backends …` in the log | Informational. The miner-api clamps concurrency to 1 when publicly reachable to mitigate SSRF risk. Override `MINERU_API_MAX_CONCURRENT_REQUESTS` if you bind to a private interface. |
| First request slow (~14 s), subsequent ones fast (~0.7 s) | Cold-start model load; the VLM is preloaded at startup (`--enable-vlm-preload true`) but the first parse still pays hybrid-engine init. |
| `mineru-models-download` prompts interactively for source + model type | Playbook fix: pipe `auto\nall\n` to stdin (already in `setup-mineru.yml`). If running by hand, just press Enter twice. |

## Logs

- **macOS node**: `/opt/homebrew/var/log/mineru.log` (mode 0640, dev:admin).
  Both stdout and stderr from `mineru-api` are captured here by the
  LaunchDaemon (mirrors the TEI log convention).
- **storage leader**: nginx `/var/log/nginx/access.log` for the
  incoming `/v1/file_parse` POSTs; `journalctl -u local-ai-proxy`
  for proxy-side routing decisions.

## See also

- [`docs/design/mineru.md`](../design/mineru.md) — technology choice,
  MLX-on-Mac rationale, why not Docker, alternatives considered.
- [`docs/usage/document-extraction.md`](../usage/document-extraction.md) —
  client-facing API: form fields, output formats, examples.
