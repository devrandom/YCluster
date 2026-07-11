import json
import threading
import time
from pathlib import Path
from typing import Iterable

_LOCK = threading.Lock()
_PATH: Path | None = None


def init(path: Path) -> None:
    global _PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    _PATH = path


def _now_iso() -> str:
    s, frac = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), time.time() % 1
    return f"{s}.{int(frac * 1000):03d}Z"


def _write(event: dict) -> None:
    if _PATH is None:
        return
    payload = {"ts": _now_iso(), **event}
    line = json.dumps(payload, separators=(",", ":")) + "\n"
    with _LOCK:
        with _PATH.open("a", encoding="utf-8") as f:
            f.write(line)


def start(session: str, host: str, cmd: str, cwd: str) -> None:
    _write({"event": "start", "session": session, "host": host, "cmd": cmd, "cwd": cwd})


def line(session: str, stream: str, text: str) -> None:
    _write({"event": "line", "session": session, "stream": stream, "line": text})


def end(
    session: str,
    exit_code: int | None,
    duration_s: float,
    status: str,
    lines: int,
    error: str | None = None,
) -> None:
    payload: dict = {
        "event": "end",
        "session": session,
        "exit": exit_code,
        "duration_s": round(duration_s, 3),
        "status": status,
        "lines": lines,
    }
    if error is not None:
        payload["error"] = error
    _write(payload)


def tail(path: Path, n: int, session: str | None = None) -> list[dict]:
    if n < 0:
        raise ValueError(f"n must be non-negative, got {n}")
    if n == 0:
        return []
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    parsed: list[dict] = []
    for line in text.splitlines():
        if not line:
            continue
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if session is not None:
        parsed = [e for e in parsed if e.get("session") == session]
    parsed = parsed[-n:]
    return parsed


def render(events: Iterable[dict], path: Path, requested: int) -> str:
    events = list(events)
    n = len(events)
    label = "event" if n == 1 else "events"
    header = f"[tail] {n} {label} from {path} (tail={requested})"
    if not events:
        return f"{header}\n\n(no events)"
    body = "\n".join(render_event(e) for e in events)
    return f"{header}\n\n{body}"


def render_event(e: dict) -> str:
    ts = e.get("ts", "?")
    event = e.get("event", "?")
    session = e.get("session", "?")
    head = f"{ts}  [{session}]  "
    if event == "start":
        host = e.get("host", "?")
        cmd = e.get("cmd", "?")
        cwd = e.get("cwd", "")
        suffix = f"cwd={cwd}" if cwd else ""
        body = f"start   host={host} cmd={cmd!r}"
        if suffix:
            body += f" {suffix}"
        return head + body
    if event == "line":
        stream = e.get("stream", "?")
        line = e.get("line", "")
        if stream == "stderr":
            return head + f"line    [stderr] {line}"
        return head + f"line    {line}"
    if event == "end":
        bits = [
            f"exit={e.get('exit', '?')}",
            f"duration={e.get('duration_s', '?')}s",
            f"status={e.get('status', '?')}",
            f"lines={e.get('lines', '?')}",
        ]
        if "error" in e:
            bits.append(f"error={e['error']!r}")
        return head + "end     " + " ".join(bits)
    return head + f"???      event={event}"
