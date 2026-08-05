"""Best-effort command validation for the cluster-exec `run` tool.

This is intended to be helpful to the agent, not adversarial: it scans the
*top-level* commands of a run command and warns the agent that a construct is
not supported (and what to use instead). Nested content -- quoted strings,
`$(...)` / `( ... )` subshells, and backticks -- is skipped so legitimate
uses like `bash -c "head x"` or `x=$(tail -1 f)` are not flagged.
"""

from __future__ import annotations

import re
import shlex

# Leading-command separators: a new command starts after any of these.
_SEPARATORS = {"|", ";", "&&", "||", "&", "|&"}

# sleep duration in seconds; 0.5, 10, 1m, 90s, 1h, ... Only the leading number
# and an optional unit suffix are considered.
_SLEEP_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd])?\s*$")

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# Command -> (denial message, allowed-if-first-arg-equals). If allowed_arg is
# set and the command's first top-level argument equals it, the command runs.
_DENY: dict[str, tuple[str, str | None]] = {
    "ssh": ("ssh is not allowed in run() - use the hostname argument to target a host instead", None),
    "head": ("head is not allowed in run() - use the run() `head_lines` arg to keep just the first lines (or `head -1` in-command)", "-1"),
    "tail": ("tail is not allowed in run() - use the run() `tail_lines` arg to keep just the last lines (or `tail -1` in-command)", "-1"),
    "headtail": ("headtail is not allowed in run()", None),
}


def _duration_seconds(word: str) -> float | None:
    """Return sleep duration in seconds; None if the argument isn't a clear number."""
    m = _SLEEP_RE.match(word)
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2)
    return value * _UNIT_SECONDS.get(unit, 1)


def validate_command(command: str) -> str | None:
    """Return a denial message for the command, or None if it should run."""
    sh = shlex.shlex(command, posix=True, punctuation_chars=True)
    sh.whitespace_split = False
    try:
        tokens = list(sh)
    except ValueError:
        # Unbalanced quoting - not our job to police; let it run.
        return None

    depth = 0          # subshell / command-substitution nesting
    in_backtick = False
    at_command_start = True

    for i, tok in enumerate(tokens):
        # Track nesting first so nested commands are skipped.
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth = max(depth - 1, 0)
        elif tok == "`":
            in_backtick = not in_backtick
            continue
        if depth > 0 or in_backtick:
            continue

        if tok in _SEPARATORS:
            at_command_start = True
            continue

        if not at_command_start:
            continue

        # We are at the start of a top-level command.
        at_command_start = False

        if tok == "sleep":
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            secs = _duration_seconds(nxt)
            if secs is None or secs > 10:
                detail = f" ({nxt} = {secs:.0f}s)" if secs is not None else ""
                return f"sleep > 10s is not allowed in run() - short sleeps only{detail}"
            continue

        deny, allowed_arg = _DENY.get(tok, (None, None))
        if deny is None:
            continue

        if allowed_arg is not None:
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            if nxt == allowed_arg:
                continue

        return deny

    return None
