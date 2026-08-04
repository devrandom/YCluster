# Contributing

## Guidelines

- Keep focused, terse docs; put more specific knowledge in `./docs/`.
- Avoid doing things "manually" in prod when there is a best-practice way of
  doing it (e.g. running a playbook).
- Playbooks must be idempotent and minimize spurious change detection.
- Do not swallow errors or stderr output.
- Prefer Python libraries over subprocess for mainstream tasks.
- Use JSON format for command output when available (e.g. `rbd --format json`).
  Add JSON output format to custom scripts used by other scripts.
- Keep scripts in `config/ansible/scripts/`, installed by playbooks.
- Use `git mv` for file renames.
- `research/`, `bench-results/`, `go-bench`, and `env.sh` are local files — do
  not commit them.
- If you need to investigate third-party code, clone it in `./ext/`.

## Commit conventions

- No commit message prefixes (no "feat:", "fix:", etc.). Be terse; short
  subjects; no rationale essays in bodies. Rationale that needs to survive
  goes in `docs/`.
- Do not create commits unless explicitly asked.
- Amend when fixing code that was recently committed.
- Run the relevant tests/build before committing. If the local toolchain
  doesn't have what's needed, stop and tell the user — never commit untested
  code.

## Local conventions

- Use `./tmp/`, not `/tmp/`, for local scratch files (logs, system-test
  output, temp configs). `./tmp/` is gitignored; system-test logs at
  `./tmp/system-test-<section>.log` follow this convention.
- Pipe long command output locally through `headtail` (on PATH at
  `~/bin/headtail`) instead of `head`/`tail` so the start and end of the
  output are both visible. Use `headtail -l FILE` to also tee the full
  un-elided output to a log file.
- Do not use `tail`/`headtail` inside the cluster-exec tool — it already trims
  long output.
- Never `rm -rf` mount points or paths that could contain user data without
  confirming with the user first — use `rmdir` for empty directories or verify
  unmounted state first.

## Third-party binaries policy

No prebuilt third-party binaries. Cluster software comes from one of:
- **apt** (Ubuntu repos)
- **snap** (held, upgraded via playbooks — see `docs/operations/packages.md`)
- **Build from source** in a dedicated `app/build-<name>.yml` or
  `admin/build-<name>.yml` playbook, source vendored under `/opt/src/`,
  pinned to a git SHA (e.g. `app/build-rathole.yml`, `app/build-open-webui.yml`)
- **Docker images** built in-cluster, pushed to `registry.xc:5000`

No `get_url`/`curl`/`wget` of compiled artifacts, no PyPI C-extension wheels,
no `curl | bash`. If a third-party binary is genuinely needed, add a
`build-<name>.yml` playbook — never `get_url` a binary into an install play.
See also `docs/operations/packages.md`.
