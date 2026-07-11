import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

from . import log


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cetail",
        description="Tail the cluster-exec JSONL log as a human-readable transcript.",
    )
    parser.add_argument(
        "-n",
        "--tail-lines",
        type=int,
        default=50,
        help="Number of events to show from the tail (default: 50).",
    )
    parser.add_argument(
        "-s",
        "--session",
        default=None,
        help="Show only events for this session ID.",
    )
    parser.add_argument(
        "--log-path",
        default=os.environ.get("CE_LOG_PATH", "tmp/cluster.log"),
        help="Path to the JSONL log (default: $CE_LOG_PATH or ./tmp/cluster.log).",
    )
    parser.add_argument(
        "-f",
        "--follow",
        action="store_true",
        help="Keep watching the log and print new events as they arrive (Ctrl-C to stop).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.25,
        help="Polling interval in seconds for -f (default: 0.25).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.tail_lines < 0:
        print(
            f"ERROR: tail_lines must be non-negative, got {args.tail_lines}",
            file=sys.stderr,
        )
        return 2
    if args.interval <= 0:
        print(
            f"ERROR: interval must be positive, got {args.interval}",
            file=sys.stderr,
        )
        return 2
    path = Path(args.log_path)
    if args.follow:
        return follow(
            path=path,
            n=args.tail_lines,
            session=args.session,
            interval=args.interval,
        )
    events = log.tail(path, n=args.tail_lines, session=args.session)
    print(log.render(events, path=path, requested=args.tail_lines))
    return 0


def follow(
    path: Path,
    n: int,
    session: str | None,
    interval: float,
    stop_event: threading.Event | None = None,
) -> int:
    while not path.exists():
        if stop_event is not None and stop_event.is_set():
            return 0
        time.sleep(interval)

    sys.stdout.write(
        f"[cetail] following {path} (tail={n}) — Ctrl-C to stop\n\n"
    )
    sys.stdout.flush()

    for event in log.tail(path, n=n, session=session):
        sys.stdout.write(log.render_event(event) + "\n")
    sys.stdout.flush()

    with path.open("r", encoding="utf-8") as f:
        f.seek(0, 2)
        buf = ""
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    return 0
                chunk = f.read()
                if chunk:
                    buf += chunk
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        if not line.strip():
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if session is not None and event.get("session") != session:
                            continue
                        sys.stdout.write(log.render_event(event) + "\n")
                        sys.stdout.flush()
                else:
                    time.sleep(interval)
        except KeyboardInterrupt:
            sys.stdout.write("\n[cetail] stopped\n")
            sys.stdout.flush()
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
