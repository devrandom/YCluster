"""Patch WhisperX 3.7.4 for pyannote.audio 4.0.x compatibility.

Two breaks vs. pyannote 3.x:
  1. `use_auth_token` kwarg renamed to `token` (handled by the sed pass).
  2. SpeakerDiarization pipeline now returns a `DiarizeOutput` dataclass
     instead of an `Annotation`, and the `return_embeddings=True` path
     no longer unpacks into a 2-tuple.

This script edits whisperx/diarize.py in place. It is idempotent.
"""
import sys
from pathlib import Path

target = Path("/opt/venv/lib/python3.12/site-packages/whisperx/diarize.py")
src = target.read_text()

# Already patched?
if "speaker_diarization" in src and "DiarizeOutput" in src:
    print(f"[patch] {target} already patched")
    sys.exit(0)

# pyannote 4.x returns DiarizeOutput from both call paths; embeddings live on it.
old_a = """        if return_embeddings:
            diarization, embeddings = self.model(
                audio_data,
                num_speakers=num_speakers,
                min_speakers=min_speakers,
                max_speakers=max_speakers,
                return_embeddings=True,
            )
        else:
            diarization = self.model(
                audio_data,
                num_speakers=num_speakers,
                min_speakers=min_speakers,
                max_speakers=max_speakers,
            )
            embeddings = None"""

new_a = """        # pyannote 4.x: DiarizeOutput dataclass replaces (Annotation, embeddings) tuple.
        _out = self.model(
            audio_data,
            num_speakers=num_speakers,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
            return_embeddings=return_embeddings,
        )
        from pyannote.audio.pipelines.speaker_diarization import DiarizeOutput
        if isinstance(_out, DiarizeOutput):
            diarization = _out.speaker_diarization
            embeddings = _out.speaker_embeddings
        else:
            diarization = _out
            embeddings = None"""

if old_a not in src:
    sys.exit(f"[patch] ERROR: anchor block not found in {target}; whisperx version drift?")

src = src.replace(old_a, new_a)
target.write_text(src)
print(f"[patch] {target} patched for pyannote 4.x DiarizeOutput")
