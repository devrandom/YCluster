def truncate(lines: list[str], head: int, tail: int) -> str:
    n = len(lines)
    if head < 0 or tail < 0:
        raise ValueError(f"head/tail must be non-negative, got head={head} tail={tail}")
    if n <= head + tail:
        return "\n".join(lines)
    head_part = lines[:head]
    tail_part = lines[-tail:] if tail > 0 else []
    elided = n - head - tail
    marker = f"… [{elided} lines elided] …"
    parts: list[str] = list(head_part)
    parts.append(marker)
    if tail_part:
        parts.extend(tail_part)
    return "\n".join(parts)
