"""OpenAI-compatible HTTP server for WhisperX on ROCm.

Endpoints:
  POST /v1/audio/transcriptions   (multipart/form-data, OpenAI-compatible)
  POST /v1/audio/translations     (translate to English)
  GET  /v1/models                 (lists the configured whisper model)
  GET  /healthz                   (liveness + model_loaded flag)

OpenAI-compatible form fields:
  file                multipart audio file (required)
  model               whisper model id (informational; the loaded model is used)
  language            ISO-639-1 (optional; auto-detect if absent)
  response_format     json | text | srt | vtt | verbose_json   (default: json)
  temperature         float                                    (default: 0)
  prompt              initial prompt (passed as initial_prompt)

WhisperX extensions (non-OpenAI):
  align               bool, default true (wav2vec2 word alignment)
  diarize             bool, default false (pyannote diarization)
  min_speakers        int, optional
  max_speakers        int, optional

Loaded models are cached in memory after first use. Cold start ~30 s,
subsequent requests are inference-only.

The server does NOT need HF_TOKEN at runtime — gated diarization models
must be pre-cached into HF_HOME by a one-time bootstrap step (see
bootstrap.sh). Container runs with HF_HUB_OFFLINE=1 so pyannote loads
straight from cache.

Env config (read at startup):
  WHISPERX_MODEL          default: large-v3
  WHISPERX_DEVICE         default: cuda
  WHISPERX_COMPUTE_TYPE   default: float16
  WHISPERX_DIARIZE_MODEL  default: pyannote/speaker-diarization-community-1
  WHISPERX_DIARIZE        true|false; preloads diarizer at startup (default: true)
  WHISPERX_HOST           default: 0.0.0.0
  WHISPERX_PORT           default: 9000
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import torch
import whisperx
import whisperx.diarize
from fastapi import FastAPI, Form, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse, PlainTextResponse

log = logging.getLogger("whisperx-server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

MODEL_ID = os.environ.get("WHISPERX_MODEL", "large-v3")
DEVICE = os.environ.get("WHISPERX_DEVICE", "cuda")
COMPUTE_TYPE = os.environ.get("WHISPERX_COMPUTE_TYPE", "float16")
DIARIZE_MODEL_ID = os.environ.get("WHISPERX_DIARIZE_MODEL", "pyannote/speaker-diarization-community-1")
HOST = os.environ.get("WHISPERX_HOST", "0.0.0.0")
PORT = int(os.environ.get("WHISPERX_PORT", "9000"))

# Coarse global lock — Whisper/pyannote backends aren't thread-safe and
# would thrash the single iGPU anyway.
_lock = threading.Lock()

_state: dict[str, Any] = {
    "asr": None,
    "align": {},              # language -> (model, metadata)
    "diarize": None,
}


def _load_asr():
    if _state["asr"] is None:
        log.info("loading ASR model=%s device=%s compute=%s", MODEL_ID, DEVICE, COMPUTE_TYPE)
        _state["asr"] = whisperx.load_model(
            MODEL_ID, device=DEVICE, compute_type=COMPUTE_TYPE,
            vad_method=os.environ.get("WHISPERX_VAD_METHOD", "silero"),
        )
    return _state["asr"]


def _load_align(language: str):
    if language not in _state["align"]:
        log.info("loading alignment model for language=%s", language)
        model, metadata = whisperx.load_align_model(language_code=language, device=DEVICE)
        _state["align"][language] = (model, metadata)
    return _state["align"][language]


def _load_diarize():
    if _state["diarize"] is None:
        # Pyannote reads the model from the local HF cache when
        # HF_HUB_OFFLINE=1 is set; no runtime token needed for gated
        # models that have been pre-cached by bootstrap.sh.
        log.info("loading diarization model=%s (offline=%s)",
                 DIARIZE_MODEL_ID, os.environ.get("HF_HUB_OFFLINE"))
        _state["diarize"] = whisperx.diarize.DiarizationPipeline(
            model_name=DIARIZE_MODEL_ID, token=None, device=DEVICE,
        )
    return _state["diarize"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Eagerly warm models so the first request isn't a ~30s outlier.
    with _lock:
        _load_asr()
        if _truthy(os.environ.get("WHISPERX_DIARIZE", "true")):
            try:
                _load_diarize()
            except Exception as e:
                log.warning("diarizer preload failed: %s; diarize requests will fail at call time", e)
    log.info("ready: model=%s device=%s", MODEL_ID, DEVICE)
    yield


app = FastAPI(title="whisperx-rocm", lifespan=lifespan)


def _truthy(v: str | None) -> bool:
    return str(v).lower() in {"1", "true", "yes", "on"}


def _build_text(segments: Iterable[dict]) -> str:
    return "\n".join(s.get("text", "").strip() for s in segments).strip()


def _build_srt(segments: Iterable[dict]) -> str:
    def ts(t: float) -> str:
        h, rem = divmod(t, 3600)
        m, s = divmod(rem, 60)
        ms = int(round((s - int(s)) * 1000))
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{ms:03d}"
    out = []
    for i, seg in enumerate(segments, 1):
        out.append(str(i))
        out.append(f"{ts(seg['start'])} --> {ts(seg['end'])}")
        out.append(seg.get("text", "").strip())
        out.append("")
    return "\n".join(out)


def _build_vtt(segments: Iterable[dict]) -> str:
    def ts(t: float) -> str:
        h, rem = divmod(t, 3600)
        m, s = divmod(rem, 60)
        ms = int(round((s - int(s)) * 1000))
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d}.{ms:03d}"
    out = ["WEBVTT", ""]
    for seg in segments:
        out.append(f"{ts(seg['start'])} --> {ts(seg['end'])}")
        out.append(seg.get("text", "").strip())
        out.append("")
    return "\n".join(out)


def _format_response(result: dict, response_format: str):
    segments = result.get("segments", [])
    if response_format == "json":
        return JSONResponse({"text": _build_text(segments)})
    if response_format == "text":
        return PlainTextResponse(_build_text(segments))
    if response_format == "srt":
        return PlainTextResponse(_build_srt(segments), media_type="application/x-subrip")
    if response_format == "vtt":
        return PlainTextResponse(_build_vtt(segments), media_type="text/vtt")
    if response_format == "verbose_json":
        return JSONResponse({
            "task": result.get("task", "transcribe"),
            "language": result.get("language"),
            "duration": result.get("duration"),
            "text": _build_text(segments),
            "segments": segments,
        })
    raise HTTPException(400, f"unsupported response_format: {response_format}")


async def _run(
    file: UploadFile,
    *,
    task: str,
    language: str | None,
    response_format: str,
    temperature: float,
    prompt: str | None,
    align: bool,
    diarize: bool,
    min_speakers: int | None,
    max_speakers: int | None,
):
    if response_format not in {"json", "text", "srt", "vtt", "verbose_json"}:
        raise HTTPException(400, f"unsupported response_format: {response_format}")

    # whisperx.load_audio reads from a path via ffmpeg.
    suffix = Path(file.filename or "audio").suffix or ".bin"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        with _lock:
            asr = _load_asr()
            audio = whisperx.load_audio(tmp_path)

            asr_kwargs = {"task": task}
            if language:
                asr_kwargs["language"] = language
            # whisperx exposes initial_prompt through faster-whisper.
            if prompt:
                asr_kwargs["initial_prompt"] = prompt
            if temperature:
                asr_kwargs["temperature"] = temperature

            result = asr.transcribe(audio, **asr_kwargs)
            detected_language = result.get("language") or language or "en"

            if align:
                try:
                    align_model, align_metadata = _load_align(detected_language)
                    aligned = whisperx.align(
                        result["segments"], align_model, align_metadata, audio,
                        DEVICE, return_char_alignments=False,
                    )
                    result["segments"] = aligned["segments"]
                except Exception as e:
                    log.warning("alignment failed for language=%s: %s; returning unaligned",
                                detected_language, e)

            if diarize:
                diarize_model = _load_diarize()
                kwargs = {}
                if min_speakers is not None:
                    kwargs["min_speakers"] = min_speakers
                if max_speakers is not None:
                    kwargs["max_speakers"] = max_speakers
                diarize_df = diarize_model(audio, **kwargs)
                result = whisperx.assign_word_speakers(diarize_df, result)

            result["language"] = detected_language
            result.setdefault("task", task)
            result["duration"] = float(len(audio) / 16000.0)
            return _format_response(result, response_format)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


@app.post("/v1/audio/transcriptions")
async def transcriptions(
    file: UploadFile = File(...),
    model: str = Form(MODEL_ID),
    language: str | None = Form(None),
    response_format: str = Form("json"),
    temperature: float = Form(0.0),
    prompt: str | None = Form(None),
    align: str = Form("true"),
    diarize: str = Form("false"),
    min_speakers: int | None = Form(None),
    max_speakers: int | None = Form(None),
):
    return await _run(
        file, task="transcribe", language=language, response_format=response_format,
        temperature=temperature, prompt=prompt,
        align=_truthy(align), diarize=_truthy(diarize),
        min_speakers=min_speakers, max_speakers=max_speakers,
    )


@app.post("/v1/audio/translations")
async def translations(
    file: UploadFile = File(...),
    model: str = Form(MODEL_ID),
    response_format: str = Form("json"),
    temperature: float = Form(0.0),
    prompt: str | None = Form(None),
):
    return await _run(
        file, task="translate", language=None, response_format=response_format,
        temperature=temperature, prompt=prompt,
        align=False, diarize=False, min_speakers=None, max_speakers=None,
    )


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "whisperx"}]}


@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "model": MODEL_ID,
        "device": DEVICE,
        "compute_type": COMPUTE_TYPE,
        "cuda_available": torch.cuda.is_available(),
        "model_loaded": _state["asr"] is not None,
        "align_languages": sorted(_state["align"].keys()),
        "diarize_loaded": _state["diarize"] is not None,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
