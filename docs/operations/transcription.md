# Audio Transcription (WhisperX on ROCm)

YCluster runs **WhisperX** on AMD Strix Halo (gfx1151) compute nodes
as an OpenAI-compatible audio endpoint. Routing goes through the
same inference gateway as chat completions, so OpenAI client
libraries reach it with no special configuration.

## What it does

- **Transcription** with `faster-whisper large-v3` on ROCm 7.2.2 —
  fp16 inference on the iGPU.
- **Word-level alignment** via wav2vec2 (per-word timestamps).
- **Speaker diarization** via `pyannote/speaker-diarization-community-1`.
- **Translation to English** (`/v1/audio/translations`).

See [`ext/whisperx.md`](../../ext/whisperx.md) for the underlying
container-strategy report this is built on.

## Usage

The cluster registers two model names that both point at the same
backend:

- `whisper-1` — the canonical OpenAI name (use this from OpenAI SDKs).
- `large-v3` — the actual underlying model (explicit; reserves a slot
  if we add other variants later).

```bash
export OPENAI_API_KEY=sk-<your-openwebui-key>
export OPENAI_BASE_URL=http://inference.xc/v1   # or https://your-domain.com/v1

# Plain transcription
curl -s -X POST "$OPENAI_BASE_URL/audio/transcriptions" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -F file=@meeting.wav \
  -F model=whisper-1 \
  -F response_format=text
```

Python:

```python
from openai import OpenAI
client = OpenAI()
with open("meeting.wav", "rb") as f:
    print(client.audio.transcriptions.create(model="whisper-1", file=f).text)
```

### OpenAI-standard form fields

| Field             | Description                                                 |
|-------------------|-------------------------------------------------------------|
| `file`            | Binary audio (wav, mp3, m4a, flac, opus, ogg, webm)         |
| `model`           | `whisper-1` or `large-v3`                                   |
| `language`        | ISO-639-1 (auto-detect if omitted)                          |
| `response_format` | `json` \| `text` \| `srt` \| `vtt` \| `verbose_json`        |
| `temperature`     | Sampling temperature (default 0)                            |
| `prompt`          | Initial prompt to bias decoding                             |

### WhisperX extensions (not in the OpenAI spec)

| Field           | Default | Description                                       |
|-----------------|---------|---------------------------------------------------|
| `align`         | `true`  | Run wav2vec2 word alignment                       |
| `diarize`       | `false` | Run pyannote speaker diarization                  |
| `min_speakers`  | —       | Lower bound on diarized speaker count             |
| `max_speakers`  | —       | Upper bound on diarized speaker count             |

`response_format=verbose_json` is what to ask for if you want word
timestamps or speaker labels — the OpenAI-compatible `json` format
returns only the flat `text`.

```bash
# Diarized verbose output (word timestamps + speaker turns)
curl -s -X POST "$OPENAI_BASE_URL/audio/transcriptions" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -F file=@meeting.wav \
  -F model=whisper-1 \
  -F response_format=verbose_json \
  -F diarize=true \
  -F max_speakers=4
```

### Translation

```bash
# Translate any source language to English
curl -s -X POST "$OPENAI_BASE_URL/audio/translations" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -F file=@japanese.wav \
  -F model=whisper-1 \
  -F response_format=text
```

## Limits

- **Upload size**: 200 MiB at the nginx vhost (`client_max_body_size`
  in `inference-internal.conf`) and at the proxy
  (`maxMultipartBodyBytes` in `local-ai-proxy/router.go`). Bumping one
  without the other gives misleading 413s.
- **Long audio**: verified stable up to 30 min on a single GPU. The
  WhisperX upstream guide does not yet report 1 h+ runs on gfx1151;
  for safety, chunk longer recordings application-side.
- **Concurrency**: the server takes a coarse global lock around each
  inference (the iGPU is the bottleneck anyway). Concurrent requests
  serialize.

## Architecture

```
client → inference.xc/v1/audio/...   (nginx, auth_request)
       → local-ai-proxy :4001        (model routing, multipart-aware)
       → <whisperx-host>:9000        (whisperx-rocm container)
         └─ faster-whisper / pyannote / wav2vec2 on gfx1151
```

- **Container image**: `localhost/whisperx-rocm:v4.7.1-gfx1151`,
  built locally from
  `config/ansible/app/files/whisperx/Containerfile`.
- **Service**: `whisperx.service` — a podman Quadlet user unit under
  `dev` on each opted-in host. Linger-enabled, restarts on failure.
- **Models**: pyannote diarization is HF-gated; the gated model is
  pre-cached into `~dev/.cache/huggingface` once with a token. The
  container then runs with `HF_HUB_OFFLINE=1` so no token is ever
  needed at runtime.
- **Proxy**: `local-ai-proxy` routes `/v1/audio/*` requests by
  parsing the multipart `model` form field (same routing key the
  JSON endpoints use, just discovered differently).
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

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `413 Request Entity Too Large` | Either nginx (`client_max_body_size`) or the proxy (`maxMultipartBodyBytes`) is below the request size. |
| `no healthy backend for model: whisper-1` | The whisperx container is down or unreachable; check `systemctl --user status whisperx` on the whisperx host and `ycluster inference status` from a storage node. |
| `Failed to load audio: ffmpeg ... Invalid data` on the server | Upload was 0 bytes (e.g. `curl` without `-L` on a redirect). Verify the source. |
| Diarization missing from output (no `speaker` keys) | `diarize=true` not sent, or pyannote model missing from `~dev/.cache/huggingface` on the whisperx host. Run `huggingface-cli download pyannote/speaker-diarization-community-1` as dev with a token, then restart the service. |
| First request slow (~30 s) but subsequent ones fast | Cold-start model load; wav2vec2 alignment models download per-language on first use. |
