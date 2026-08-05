# DONE

Completed or no-longer-relevant items. The files live under
`archive/`; re-open by moving the file back to a subject-matter folder
and removing the entry here.

- [Sandbox MinerU's outbound](archive/sandbox-mineru-outbound.md) — Linux/Nvidia done (`e5ecb1b` / `0f8729a`); macOS deferred, `setup-mineru.yml` now hard-aborts until the Seatbelt profile lands.
- [Reproducible build of b12x SM120 inference image](archive/b12x-reproducible-build.md) — moot; we dropped b12x for stock vLLM `0.24`, which carries upstream's native `minimax_m3` support (PR #45381).
- [MiniMax-M3 reasoning parser](archive/m3-reasoning-parser.md) — moot; stock vLLM `0.24` carries the built-in `minimax_m3` reasoning parser, so the local plugin (`/opt/inference/minimax-m3/minimax_m3_reasoning_parser.py`) is now inert.
- [Move API key generation out of Open WebUI](archive/move-api-key-gen-out-of-openwebui.md) — done; gateway keys are minted/hashed in etcd (`/cluster/users/<email>/keys/`, argon2id) and validated by `local-ai-proxy-auth` with no OWUI postgres hop; migration copied existing OWUI keys into the new schema so tokens keep working; OWUI-key lockdown deliberately not done (OWUI keys remain for OWUI sign-in only).
- [Vendor account cleanup](archive/vendor-account-cleanup.md) — `exx` removed on nv2/nv5 + cluster key removed from `authorized_keys.disabled` (2026-07-25, private `todos.md`); nvidia/nas/macos and the remote wg/wg-macos bootstraps now fail on any non-baseline sudo/lxd/admin account before provisioning. Only operator-manual `foresight` removal (c3/m3) and `ubuntu`/nv3 remain, tracked under the `sudo-user-consistency` item.
