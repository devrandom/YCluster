#!/usr/bin/env python3
"""Generate a per-worktree .codex/config.toml from the main checkout's.

Linked worktrees share the main repo's .git, whose writable root is registered in
the shared .codex/config.toml. Codex resolves a worktree's .git pointer file and
marks the resolved gitdir (~/.git/worktrees/<name>) read-only by default; because
that carveout is more specific than the shared `~/proj/ycluster/.git` writable
root, it wins and git fails inside the worktree. Registering the exact worktree
gitdir as an explicit writable root suppresses the carveout.

Usage: dev/worktree-config.py <main_root> <worktree_dir>
"""
import argparse
import sys
from pathlib import Path


def inject_gitdir(text: str, gitdir: str) -> str:
    needle = f"writable_roots"
    lines = text.splitlines()
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(needle) and "=" in stripped and "[" in stripped:
            if f'"{gitdir}"' in line:
                out.append(line)
                continue
            # insert before the closing bracket, keep existing entries
            outs = line.rstrip()
            if outs.endswith("]"):
                outs = outs[:-1].rstrip() + ", "
                outs += f'"{gitdir}"]'
            else:
                outs += f', "{gitdir}"'
            out.append(outs)
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("main_root", help="path to the main checkout root")
    ap.add_argument("worktree", help="path to the worktree being configured")
    args = ap.parse_args()

    main = Path(args.main_root).resolve()
    wt = Path(args.worktree).resolve()
    name = wt.name
    src = main / ".codex" / "config.toml"
    dst_dir = wt / ".codex"
    dst = dst_dir / "config.toml"

    if not src.is_file():
        print(f"error: main config not found: {src}", file=sys.stderr)
        return 1
    dst_dir.mkdir(parents=True, exist_ok=True)

    gitdir = str(main / ".git" / "worktrees" / name)
    text = src.read_text()
    new_text = inject_gitdir(text, gitdir)

    # Replace any existing real file or symlink.
    if dst.is_symlink() or dst.exists():
        dst.unlink()
    dst.write_text(new_text)
    print(f"wrote {dst}")
    print(f"  writable_roots now includes {gitdir}")
    if dst.is_symlink():
        print("warning: target is still a symlink", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
