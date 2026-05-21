# Audio Transcription — Ops

WhisperX runs on AMD Strix Halo (gfx1151) compute nodes as a podman
rootless service, fronted by `local-ai-proxy` and the `inference.xc`
nginx vhost.

For the **user-facing API surface** (endpoints, form fields, examples),
see [`docs/usage/transcription.md`](../usage/transcription.md). This
page covers deployment, architecture, and troubleshooting only.

See [`ext/whisperx.md`](../../ext/whisperx.md) for the underlying
container-strategy report this is built on.

## Architecture

```
client → inference.xc/v1/audio/...   (nginx, auth_request)
       → local-ai-proxy :4001        (model routing, multipart-aware)
       → <whisperx-host>:9000        (whisperx-rocm container)
         └─ faster-whisper / pyannote / wav2vec2 on gfx1151
```

- **Container image**: `localhost/whisperx-rocm:v4.7.1-gfx1151`,
  built locally from
  `config/ansible/app/files/whisperx/Containerfile`. Ships its own
  ROCm 7.2.2 + PyTorch 2.10 + CTranslate2 4.7.1 (gfx1151 wheel), so
  the host's ROCm version is irrelevant.
- **Service**: `whisperx.service` — a podman Quadlet user unit under
  `dev` on each opted-in host. Linger-enabled, restarts on failure.
- **Models**: pyannote diarization is HF-gated; the gated model is
  pre-cached into `~dev/.cache/huggingface` once with a token. The
  container then runs with `HF_HUB_OFFLINE=1` so no token is ever
  needed at runtime.
- **Proxy**: `local-ai-proxy` routes `/v1/audio/*` requests by parsing
  the multipart `model` form field (same routing key the JSON
  endpoints use, just discovered differently).
- **Model registration** (replace `<host>` with the whisperx node):
  ```bash
  ycluster inference add http://<host>.xc:9000               # → large-v3
  ycluster inference add http://<host>.xc:9000 whisper-1     # alias
  ```

## Opting a host in

The playbook gates on the GPU local fact (`has_amd_gpu`) and on
per-host `whisperx_enabled: true` in `host_vars/<host>.yml`. Hardware
requirement: AMD Strix Halo (gfx1151) — e.g. Ryzen AI Max+ 395 with
the integrated Radeon 8060S, ~96 GB UMA. Other AMD GPUs are not
supported by this build (CTranslate2's ROCm wheel ships kernels for
gfx900..gfx1151/gfx1201 but only Strix Halo has been validated here).

## Deploying / Updating

```bash
# From a core node:
ssh s3.yc "cd /etc/ansible && ./run-playbook.sh app/install-whisperx.yml --limit <host>"
```

The playbook is idempotent. It rebuilds the container image only if
the Containerfile, `server.py`, or any patch file changed; restarts
the systemd user unit only when the Quadlet template or image hash
changed.

## Body-size knobs

200 MiB is the max upload size, set in **two** places that must stay
in lockstep:

- nginx `client_max_body_size` in
  `config/ansible/app/files/inference-internal.conf`.
- `maxMultipartBodyBytes` in `local-ai-proxy/router.go`.

Bumping one without the other gives misleading 413s.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `413 Request Entity Too Large` | Either nginx (`client_max_body_size`) or the proxy (`maxMultipartBodyBytes`) is below the request size. |
| `no healthy backend for model: whisper-1` | The whisperx container is down or unreachable; check `systemctl --user status whisperx` on the whisperx host and `ycluster inference status` from a storage node. |
| `Failed to load audio: ffmpeg ... Invalid data` on the server | Upload was 0 bytes (e.g. `curl` without `-L` on a redirect). Verify the source. |
| Diarization missing from output (no `speaker` keys) | `diarize=true` not sent, or pyannote model missing from `~dev/.cache/huggingface` on the whisperx host. Run `huggingface-cli download pyannote/speaker-diarization-community-1` as dev with a token, then restart the service. |
| First request slow (~30 s) but subsequent ones fast | Cold-start model load; wav2vec2 alignment models download per-language on first use. |
