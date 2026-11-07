#!/usr/bin/env python3
"""Cap the DS4.1-Flash MoET filesystem KV tier at a byte budget.

The MoET launcher gives vLLM an OffloadingConnector with a
TieringOffloadingSpec: a pinned CPU-RAM primary tier and one filesystem
secondary tier. That filesystem tier has no quota and no eviction of its
own -- every block written to the CPU tier is also written through to
``<root>/.../<hash>.bin``, and files are only removed when a short read
proves corruption. Left alone the tree grows until the host filesystem
fills, which on a GPU node also takes podman storage down with it.

This pruner walks the tier and, while the tree is over ``--max-bytes``,
deletes the oldest block files. Recent files are left alone so an
in-flight write is never yanked (the tier writes ``<name>.tmp`` then
renames, and both are outside the candidate set until they age out).

Deleting a block the engine is about to read is harmless: the tier probes
every block with a filesystem lookup, so a missing file is a cache miss
and that request simply recomputes.

Exit status is non-zero only when a deletion failed for a reason other
than the file already being gone, so the timer surfaces real errors
(permissions, read-only filesystem) instead of hiding them.
"""

import argparse
import os
import stat
import sys
import time

GIB = 1 << 30


def scan(root, min_age_seconds, now):
    """Return (total_bytes, candidates) for the tier tree.

    ``total_bytes`` counts every block file; ``candidates`` holds only the
    files old enough to delete, as (mtime, size, path) tuples.
    """
    total = 0
    candidates = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.endswith(".tmp"):
                continue
            path = os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            total += st.st_size
            if now - st.st_mtime >= min_age_seconds:
                candidates.append((st.st_mtime, st.st_size, path))
    candidates.sort()
    return total, candidates


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="filesystem tier root")
    parser.add_argument("--max-bytes", type=int, required=True)
    parser.add_argument(
        "--min-age-seconds",
        type=int,
        default=300,
        help="leave blocks touched more recently than this alone (default 300)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.max_bytes <= 0:
        parser.error("--max-bytes must be positive")

    if not os.path.isdir(args.root):
        print(f"kvcache: {args.root} not present, nothing to prune")
        return 0

    total, candidates = scan(args.root, args.min_age_seconds, time.time())
    budget = args.max_bytes

    if total <= budget:
        print(
            f"kvcache: {total / GIB:.1f} GiB of {budget / GIB:.1f} GiB used,"
            " nothing to prune"
        )
        return 0

    freed = 0
    removed = 0
    failures = 0
    for _mtime, size, path in candidates:
        if total - freed <= budget:
            break
        if args.dry_run:
            print(f"kvcache: would remove {path} ({size / GIB:.3f} GiB)")
            freed += size
            removed += 1
            continue
        try:
            os.unlink(path)
        except FileNotFoundError:
            freed += size
            continue
        except OSError as exc:
            print(f"kvcache: cannot remove {path}: {exc}", file=sys.stderr)
            failures += 1
            continue
        freed += size
        removed += 1

    remaining = total - freed
    action = "would free" if args.dry_run else "freed"
    print(
        f"kvcache: {total / GIB:.1f} GiB of {budget / GIB:.1f} GiB used,"
        f" {action} {freed / GIB:.1f} GiB in {removed} blocks,"
        f" {remaining / GIB:.1f} GiB left"
    )
    if remaining > budget:
        print(
            "kvcache: budget still exceeded; the remaining blocks are inside"
            f" the {args.min_age_seconds}s write window",
            file=sys.stderr,
        )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
