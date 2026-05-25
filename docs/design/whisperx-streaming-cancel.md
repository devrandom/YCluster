# WhisperX: per-segment streaming + client-cancel propagation

## Status
Not started. Plan only. The current `server.py` runs `asr.transcribe(audio)`
inside `asyncio.to_thread`, which fixed event-loop blocking (so
`/v1/models` and `/healthz` stay responsive during a job) but does
**not** propagate client cancellation: a CPython thread can't be killed
from outside, so the GPU keeps running on an abandoned job and holds
the per-process `threading.Lock`.

## Why it matters
- Client disconnects today waste up to the full audio duration of GPU
  time. For a 40-minute file that's ~5 minutes of compute thrown away.
- The next legitimate request queues behind the orphaned job for the
  entire remaining transcribe duration.

## How cancel reaches the backend today
End-to-end propagation works through the first two hops:

| Layer | Propagates cancel? | Notes |
|---|---|---|
| nginx | yes | `proxy_ignore_client_abort off` (default). Closes upstream when client goes away. |
| local-ai-proxy | yes | `handler.go` uses `http.NewRequestWithContext(r.Context(), …)`; client disconnect cancels the upstream Go context. |
| whisperx (FastAPI) | **partially** | Starlette cancels the awaiting handler — but the `asyncio.to_thread` worker thread keeps running. |
| Whisper inference | no | No cancel hook in `faster_whisper.transcribe`. |

## The whisperx pipeline (empirical, from `ext/whisperx/whisperx/asr.py:192`)
```
FasterWhisperPipeline.transcribe(audio):
  1. VAD over the whole audio       → list of speech chunks merged to ≤ chunk_size=30 s each
  2. (optional) language detection  → ~1 s on first 30 s of audio
  3. Tokenizer setup
  4. for idx, out in self.__call__(data(audio, vad_segments), batch_size=batch_size):
       segments.append({"text": out["text"], "start": …, "end": …})
  5. return {"segments": segments, "language": language}
```

Key facts:
- Segments are **VAD-driven**, max 30 s of audio each (`chunk_size=30`).
- The inner `__call__` is HuggingFace `Pipeline.__call__`. It batches
  `batch_size` segments per GPU forward pass; the outer `for` yields one
  result per VAD segment but **GPU compute happens batch-at-a-time**.
- whisperx CLI default `batch_size = 8`. Server doesn't override, so
  HF's Pipeline default applies. Cancel granularity is therefore one
  *batch*, not one segment — worst case ~8 × 30 s = 4 min of audio per
  unkillable batch.
- A `verbose=True` parameter already exists and prints per-segment to
  stdout — proof that segments yield incrementally and we can hook in.

## Plan

### Implementation shape
Re-implement step 4 (and the surrounding scaffolding from steps 2–5)
inside our `server.py`. Trying to subclass `FasterWhisperPipeline` is
awkward because the iteration goes through HF Pipeline internals; the
cleanest path is to vendor whisperx 3.7.4's `transcribe` body
(~60 lines) and add two instrumentation points:

1. **Per-segment**: log `idx`, `[start, end]` in audio time, first ~60
   chars of text, and elapsed wall-clock since last segment.
2. **Cancel check**: between iterations, poll a `threading.Event`. If
   set, break out of the loop and proceed to "partial result" handling.

### Cancel signal wiring
- The asyncio disconnect watcher (already drafted in `server.py`) goes
  from logging-only to `cancel_event.set()`.
- The vendored loop checks `cancel_event.is_set()` after each segment.
- On cancel: skip remaining VAD chunks, skip `whisperx.align`, skip
  diarization, return what we've got.

### Response shape on cancel
Return `200` with the partial `verbose_json` payload, adding a top-level
`"partial": true` and `"segments_collected": N` field. Clients that
have already disconnected won't see it; clients that haven't (e.g.
test harnesses that close the request mid-stream) get a usable partial
transcript. Avoids a `499`-only path that's hostile to OpenAI SDKs.

### batch_size knob
Default of 8 makes cancel granularity ~30 s × 8 = ~4 min of audio. Two
viable settings:

| batch_size | Cancel granularity (audio) | Throughput cost vs default |
|---|---|---|
| 1 | up to 30 s | likely ~2× slower |
| 2 | up to 60 s | mildly slower |
| 8 (default) | up to 4 min | none |

Start with **batch_size=2** for the first iteration — meaningful cancel
responsiveness without halving throughput. Tunable via env var.

### What stays unkillable
- VAD over full audio (Silero on CPU; seconds, not minutes — fine).
- The *currently in-flight* batch of GPU inference.
- Alignment and diarization, **if** they have already started by the
  time the cancel arrives. Gate them on `cancel_event.is_set()` before
  starting; once started, they finish.

### Vendored imports we'd need
From whisperx internals:
- `whisperx.vads.Vad`, `whisperx.vads.pyannote.Pyannote`
- `whisperx.audio.SAMPLE_RATE`, `N_SAMPLES`, `log_mel_spectrogram`
- `whisperx.tokenizer.Tokenizer`
- `whisperx.utils.find_numeral_symbol_tokens`

Acceptable coupling: we already pin `whisperx==3.7.4` in the
Containerfile. If we ever bump that version we re-review this loop.

## Steps
1. Vendor the loop into `_do_transcribe`, with per-segment logging and
   `batch_size=2`. Cancel flag wired but watcher still only *logs* (no
   `.set()` yet) so behaviour is unchanged. Deploy, run a normal job,
   read the per-segment timing in logs to validate cadence assumptions.
2. Switch the disconnect watcher to call `cancel_event.set()`. Add
   partial-result return path. Test with `curl …/v1/audio/transcriptions &
   sleep 5; kill %1` and confirm:
   - GPU returns to idle within ~one batch
   - Next request is served immediately
   - Response stream is not consumed downstream (client gone)
3. Document the partial-result shape in `docs/usage/transcription.md`.

## Risks
- whisperx may revise its internals across minor versions; the vendored
  loop drifts silently. Mitigated by version pin + a comment pointing at
  the upstream file we mirror.
- Alignment on a partial segment list — should "just work" (segments are
  independent), but untested. Verify in step 2.
- VAD-batched first-yield latency: we'll see this empirically in step 1.
