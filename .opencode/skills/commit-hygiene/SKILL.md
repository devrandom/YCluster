---
name: commit-hygiene
description: Author and review YCluster commits against the repo's no-leak rules before staging, committing, or pushing. Use whenever about to stage or commit changes, when writing a commit message, or when reviewing a diff — blocks prod-private data (emails, deployment hostnames/URLs, public IPs, user VM names, prod-event dates), prebuilt third-party binaries, and unsolicited commits
---

# Commit hygiene

Gate every stage/commit/push in the `ycluster` repo. Pairs with
`git-history-cleanup` for retroactive fixes.

## Before anything

- **Don't commit unless explicitly asked.** Never create commits "for good
  measure". When fixing recently-committed code, amend instead of adding a
  commit.
- **No commit prefixes**; use a compact imperative subject line.

## Public repo must never contain

- **Emails** (prod/personal). Placeholders (`example.com`, `x.org`,
  `dev.test`) are fine.
- **Deployment hostnames / URLs / FQDNs** — especially any with index > 3
  (`c4`, `s5`, `nv4`, `s5`, `s4`) and any real hostname beyond `s1-s3`
  (`nv*`, `m1`, `m2`, …). `s1-s3` and generic placeholders (`host.ycluster.net`,
  `DOMAIN`) are fine.
- **Public IPs** (use RFC 5737 `203.0.113.x` in docs/example/tests).
- **User-chosen VM names**.
- **Dates of production events** (prod incidents/changes). Triage / eval /
  review / dev-test / benchmark dates are fine.
- **Prebuilt third-party binaries**: no `get_url`/`curl`/`wget` of compiled
  artifacts or wheels, no `curl | bash`. Source comes from a
  `build-<name>.yml` playbook pinned to a SHA, or in-cluster images.

## Pre-commit scan

Scan the staged diff before committing:

```bash
git diff --cached -U0 | grep -nEiE '\b(nv[4-9]|s[4-9]|c[4-9]|m[4-9])\b|[0-9]{1,3}(\.[0-9]{1,3}){3}|[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}'
```

Expected matches to ignore: `s1-s3` hosts, RFC 5737 `203.0.113.x`, loopback /
private / DNS server IPs (`8.8.8.8`, `1.1.1.1`), `example.com`/`x.org`/
`dev.test` placeholders, and package-version strings that look like IPs.

If a real identifier shows up, fix it in the working tree — replace hostnames
with a sanitized token and public IPs with `203.0.113.x` — and move any
prod-specific detail to `NOTES.md` (gitignored) or the sibling `ycluster-private`
repo. Do not commit with the leak.

## When an inciting incident is caught anyway

- If only the tip leaks, a normal scrub commit suffices (no history rewrite).
- If it is already pushed, treat it as a history-scrub: see
  `git-history-cleanup` and the private `history-cleanup-plan.md`; coordinate
  the force-push and re-clones, and update `replace-spec.txt` so `git-filter-repo`
  purges the token.
