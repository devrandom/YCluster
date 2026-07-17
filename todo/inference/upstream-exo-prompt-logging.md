# Upstream the exo prompt-logging fix

exo leaks prompt content into its logs (and the on-disk log file) at
INFO — `logger.info(prompt)` in `utils_mlx.py` plus
`InputMessageContent.__repr__` showing the first 100 chars, which every
task/command-object log dump recurses into. We carry a 2-hunk patch in
`config/ansible/macos/files/exo/patches/0001-no-prompt-logging.patch`;
submit it as a PR to exo-explore/exo so the carried patch can eventually
be dropped.
