# DONE

Completed or no-longer-relevant items. The files live under
`archive/`; re-open by moving the file back to a subject-matter folder
and removing the entry here.

- [Sandbox MinerU's outbound](archive/sandbox-mineru-outbound.md) — Linux/Nvidia done (`e5ecb1b` / `0f8729a`); macOS deferred, `setup-mineru.yml` now hard-aborts until the Seatbelt profile lands.
- [Reproducible build of b12x SM120 inference image](archive/b12x-reproducible-build.md) — moot; we dropped b12x for stock vLLM `0.24`, which carries upstream's native `minimax_m3` support (PR #45381).
- [MiniMax-M3 reasoning parser](archive/m3-reasoning-parser.md) — moot; stock vLLM `0.24` carries the built-in `minimax_m3` reasoning parser, so the local plugin (`/opt/inference/minimax-m3/minimax_m3_reasoning_parser.py`) is now inert.
