---
name: commit-hygiene
description: Author and review YCluster commits against the repo's no-leak rules before staging, committing, or pushing. Use whenever about to stage or commit changes, when writing a commit message, or when reviewing a diff — blocks prod-private data (emails, deployment hostnames/URLs, public IPs, user VM names, prod-event dates), fleet operational state (which machine runs which version, patch/reboot posture, rollout progress), prebuilt third-party binaries
---

# Commit hygiene

Gate every stage/commit/push in the `ycluster` repo. Pairs with
`git-history-cleanup` for retroactive fixes.

## Before anything

- Amend existing commits for fixups when practical; when fixing
  recently-committed code, amend rather than adding a separate commit.
- **No commit prefixes**; use a compact imperative subject line.

## Public repo must never contain

- **Emails** (prod/personal). Placeholders (`example.com`, `x.org`,
  `dev.test`) are fine.
- **Deployment hostnames / URLs / FQDNs** — especially any with index > 3
  (`c4`, `nv4`, `s4`) and any real hostname beyond `s1-s3`
  (`nv*`, `m1`, `m2`, …). `s1-s3` and generic placeholders (`host.example.net`,
  `DOMAIN`) are fine.
- **Public IPs** (use RFC 5737 `203.0.113.x` in docs/example/tests).
- **User-chosen VM names**.
- **Dates of production events** (prod incidents/changes). Triage / eval /
  review / dev-test / benchmark dates are fine.
- **The fleet's operational state**: which machine runs which kernel or package
  version, how many machines sit on each line, which ones have a fixed version
  installed but not booted, reboot progress, and the versions a validation
  table was measured on. Each of those looks like an innocuous package version;
  together they are the patch posture of every host, and that is what an
  attacker wants. Describe the shape instead ("hosts on the GA and HWE lines",
  "most hosts still boot the older build") and keep the per-host table in
  `ycluster-private` — including the exact versions a public validation table
  was measured on when only its numbers stay public. A version quoted from a
  public advisory ("fixed in `6.8.0-149.149`") is fine; a version attached to
  a machine is not. Example commands use `<source>=<version>`, never a version
  the fleet actually runs.
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

The hostname scan does not catch an operational-state leak, because a version
needs no hostname to be revealing. Scan for version-shaped and census-shaped
text separately:

```bash
git diff --cached -U0 | grep -nE '\b[0-9]+\.[0-9]+\.[0-9]+-[0-9]+'
git diff --cached -U0 | grep -niE '\b(one|two|three|four|five|six|seven|eight|nine|ten|[0-9]+) ([0-9]+\.[0-9]+ |[A-Za-z]+ )?hosts?\b|\bhosts? on [0-9]+\.[0-9]+'
```

For each hit ask: does this sentence tell a reader which machine runs this
version, or how many machines are behind? If yes, it belongs in the private
repo. Expected matches to ignore: advisory versions quoted in a CVE note,
test fixtures, Ubuntu release numbers (`24.04`), and design text about a
package format.

## When a leak is caught anyway

- **Only the working tree leaks**: fix it before staging; nothing else to do.
- **Committed but not pushed**: rewrite the local history. A scrub commit on
  top leaves the text readable in the log, and the rule exists so the public
  history never carries it. The tree filter below is what worked for a leaked
  fleet-state note; it is cheap because the leak is textual and confined to a
  few files.
   1. Save the sanitized files, then `git stash push -- <paths>` so the tree is
      clean; `filter-branch` refuses to run dirty.
   2. Rewrite the range with an idempotent script that applies the same
      substitutions to every commit that touches the file, tolerating commits
      that do not have the section yet:
      `git filter-branch -f --tree-filter 'python3 /abs/path/sanitize.py' -- origin/main..HEAD`
      (`git-filter-repo --replace-text` when it is installed).
   3. Verify every rewritten blob, not just the tip:
      `for c in $(git rev-list origin/main..HEAD); do git show $c:<path> | grep -nE '<pattern>'; done`.
      An intermediate commit is as public as the tip once pushed.
   4. Prove nothing else moved: keep a temporary branch at the old tip and
      `git diff --stat <old-tip> <new-tip>` must show only the intended files.
   5. Look for branches and worktrees that descend from the old commits — a
      later merge of one back into `main` reintroduces the text:
      `for r in $(git for-each-ref --format='%(refname:short)' refs/heads/); do git merge-base --is-ancestor <old> $r && echo $r; done`.
   6. Delete the temporary branch and `refs/original`; the old objects stay
      loose until reflog expiry (`git gc --prune=now` if the machine is shared).
- **Already pushed**: treat it as a history-scrub: see `git-history-cleanup`
  and the private `history-cleanup-plan.md`; coordinate the force-push and
  re-clones, and update `replace-spec.txt` so `git-filter-repo` purges the
  token.
