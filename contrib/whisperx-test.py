#!/usr/bin/env python3
"""Hit the cluster's /v1/audio/transcriptions with diarization.

Usage:
    contrib/whisperx-test.py path/to/audio.wav

Endpoint + bearer token come from config.yml at the repo root (see
contrib/_cluster_config.py).
"""
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cluster_config


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <audio-file>", file=sys.stderr)
        return 2

    audio = Path(sys.argv[1])
    if not audio.is_file():
        print(f"not a file: {audio}", file=sys.stderr)
        return 2

    cfg = _cluster_config.load()
    url = cfg.endpoint + "/v1/audio/transcriptions"

    with audio.open("rb") as f:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {cfg.token}"},
            files={"file": (audio.name, f, "application/octet-stream")},
            data={
                "model": "whisper-1",
                "response_format": "verbose_json",
                "diarize": "true",
            },
            timeout=600,
        )

    if not resp.ok:
        print(f"HTTP {resp.status_code}: {resp.text}", file=sys.stderr)
        return 1

    out = resp.json()
    print(f"language={out.get('language')} duration={out.get('duration', 0):.1f}s "
          f"segments={len(out.get('segments', []))}")
    for seg in out.get("segments", []):
        speaker = seg.get("speaker", "?")
        text = seg.get("text", "").strip()
        print(f"  [{seg['start']:6.2f}-{seg['end']:6.2f}] {speaker:11} {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
