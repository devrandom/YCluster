# MiniMax-M3 reasoning parser: proper prompt-check fix

Replace the always-think workaround with the proper prompt-check fix so
M3 can reason *adaptively*.

## Current state

The shipped `minimax_m3` reasoning parser
(`config/ansible/admin/files/minimax_m3_reasoning_parser.py`, loaded via
`--reasoning-parser-plugin`) extracts `<mm:think>…</mm:think>` into
`message.reasoning` correctly — but only when the prompt prefills the
opener. Without prefill, vLLM's one-time prompt-check
`BaseThinkingReasoningParser.is_reasoning_end(prompt)` (in vLLM's
`vllm/reasoning/basic_parsers.py`, inherited — we don't override it)
scans backward and hits the `</mm:think>` that appears *literally in the
chat template's thinking-instructions text*, latching
`reasoning_ended=True` before generation → streaming extraction disabled
→ raw tags leak to every frontend.

Current mitigation: force `thinking_mode=enabled` via
`--default-chat-template-kwargs` in `minimax-m3-b12x-start.sh`, which
guarantees the prefill but makes M3 reason on *every* turn.

## Proper fix

Override `is_reasoning_end` in our plugin (we subclass the base, so no
vLLM patching) to ignore the empty `<mm:think></mm:think>` instruction pair —
cancel an opener whose immediate next token is the closer, then return
`last_real_end > last_open` — and drop the `thinking_mode=enabled`
default. The per-delta `is_reasoning_end_streaming` already uses
generated-only tokens so it's unaffected.

## Test before shipping

3 streaming cases: adaptive-thinks, adaptive-skips, prior-turn-with-reasoning.

## Note

This whole plugin (and the override) goes away if we rebuild on a vLLM
carrying upstream's native M3 support (PR #45381, 2026-06-15, which
ships the built-in `minimax_m3` reasoning parser) — see the
reproducible-build item (`../archive/b12x-reproducible-build.md`). The
override is the fix only while we stay on this image.

## Status

MOOT. Stock vLLM `0.24` carries the upstream `minimax_m3` reasoning
parser (PR #45381), so the local plugin and the override described
here are no longer needed. The plugin on disk
(`/opt/inference/minimax-m3/minimax_m3_reasoning_parser.py`, deployed
by `install-minimax-m3.yml`) is now inert. Current prod state in
`docs/inference/minimax-m3-nvfp4-sm120.md`.

Refs:
- `docs/inference/minimax-m3-nvfp4-sm120.md`
