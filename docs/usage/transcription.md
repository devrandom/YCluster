# Audio Transcription (WhisperX)

YCluster exposes an OpenAI-compatible audio API. OpenAI client
libraries reach it with no special configuration — just point them
at the cluster's inference base URL.

Behind the scenes the cluster runs **WhisperX** (faster-whisper +
wav2vec2 alignment + pyannote diarization) on AMD Strix Halo. See
[`docs/operations/transcription.md`](../operations/transcription.md)
for the deployment and ops side. For text/LLM chat on the same
gateway, see [`inference.md`](inference.md).

## What's available

- **Transcription** with `faster-whisper large-v3`.
- **Word-level alignment** via wav2vec2 (per-word timestamps).
- **Speaker diarization** via `pyannote/speaker-diarization-community-1`.
- **Translation to English** (`/v1/audio/translations`).

The cluster registers two model names that both point at the same
backend:

- `whisper-1` — the canonical OpenAI name (use this from OpenAI SDKs).
- `large-v3` — the actual underlying model (explicit; reserves a slot
  if other variants are added later).

## Quick start

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

## OpenAI-standard form fields

| Field             | Description                                                 |
|-------------------|-------------------------------------------------------------|
| `file`            | Binary audio (wav, mp3, m4a, flac, opus, ogg, webm)         |
| `model`           | `whisper-1` or `large-v3`                                   |
| `language`        | ISO-639-1 (auto-detect if omitted)                          |
| `response_format` | `json` \| `text` \| `srt` \| `vtt` \| `verbose_json`        |
| `temperature`     | Sampling temperature (default 0)                            |
| `prompt`          | Initial prompt to bias decoding                             |

## WhisperX extensions (not in the OpenAI spec)

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

## Translation

```bash
# Translate any source language to English
curl -s -X POST "$OPENAI_BASE_URL/audio/translations" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -F file=@japanese.wav \
  -F model=whisper-1 \
  -F response_format=text
```

## Sample script

[`contrib/whisperx-test.py`](../../contrib/whisperx-test.py) is a
~50-line Python smoke test that POSTs an audio file with
`diarize=true` and prints per-segment `[start-end] SPEAKER text` lines.
Endpoint + bearer token come from `config.yml` at the repo root (see
[`contrib/_cluster_config.py`](../../contrib/_cluster_config.py)).

```bash
# config.yml at the repo root:
#   endpoint: https://your-cluster.example/
#   api_token_file: my.token

python3 contrib/whisperx-test.py audio-samples/sample.wav
```

## Limits worth knowing about

- **Upload size**: 200 MiB max per request.
- **Long audio**: validated up to 30 min in a single request. For
  longer recordings, chunk client-side.
- **Concurrency**: requests are serialized on the GPU (single
  iGPU backend). Expect linear queueing under load.
- **First request after a redeploy**: ~30 s slower while models warm
  up; subsequent requests are inference-only.
