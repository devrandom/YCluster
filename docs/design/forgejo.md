# Self-hosted git forge: Forgejo

## Status

**Trial-deployed on the storage leader, internal plus external HTTPS and SSH.**
The server, OIDC client, nginx vhosts, and CI workflow are in the tree and
running at `forgejo.xc` and `https://git.<domain>`; OIDC discovery and
the **Sign in with YCluster** redirect were verified, the app appears in the
Authentik library and the root landing page, and internal/external HTTP token
plus internal SSH-key clone/push were smoke-tested and cleaned up. The external
SSH endpoint is live at `ssh://git@git.<domain>:2222` through the leader-gated
frp TCP proxy; a key-auth clone/push still needs a user-key smoke test. The
Actions runner, the perimeter CVE manifest, and the off-cluster forge-data dump
are still pending. (Researched 2026-10-21 against Forgejo 17.0.0, Gitea
1.27.3, OneDev 16.9.5, Soft Serve 0.12.3, and current GitLab
installation requirements.)**

Decided so far:

- Adopt **Forgejo 17.0.0** as the cluster's git forge. GitLab
  Community Edition (CE) is the open-source GitLab edition; there is no
  separately named "GitLab OSS" product.
- Run it leader-gated on the storage leader, like Authentik and Open-WebUI,
  with state on `/rbd/user/forgejo` and relational state in the cluster's
  shared PostgreSQL.
- Deploy the official Forgejo container image pinned by digest, mirror it
  into `registry.xc`, and track it with the `osv-feed` container
  manifests (Alpine base packages plus the Forgejo Go module) — the same
  digest-pinned pattern as Authentik.
- Authenticate users through Authentik over OIDC. Keep one local
  break-glass administrator account and no open registration.
- Use Forgejo Actions with a repository-scoped, ephemeral runner on a
  compute node, not on the storage leader.
- Serve internally at `forgejo.xc`. The external `git.<domain>` vhost is
  gated by `forgejo_external` (default `true`) and uses the existing
  frp/nginx/certbot path. Disable it with `-e forgejo_external=false`.
- External SSH is a separate raw TCP surface on frontend port 2222, gated by
  `forgejo_external_ssh` (default `true`). It does not touch the frontend's
  own sshd on port 22. Disable it by running `install-frp-server.yml` on the
  frontend and `install-forgejo.yml` on storage with
  `-e forgejo_external_ssh=false`.
- Seed the new forge from the existing git refs. There is no issue, milestone,
  release, or wiki migration because those features are not in active use.
- Do not migrate `.gitlab-ci.yml` as-is. Rewrite the six jobs as a Forgejo
  Actions workflow and run both pipelines in parallel before cutover.

Implementation in the tree:

- `config/ansible/app/install-forgejo.yml`, `files/forgejo-manager`,
  and `files/forgejo-compose.yaml` deploy the server on the storage leader,
  create the PostgreSQL database/role, seed the digest-pinned image into
  `registry.xc`, and render `app.ini` with generated secrets. The built-in
  SSH host key is ed25519 at `/data/gitea/ssh/forgejo.ed25519`, outside
  `/data/ssh`: the image's OpenSSH service resets ownership of everything in
  `/data/ssh` to root on every container start.
- `files/authentik-manager` always writes the `forgejo` OIDC provider,
  including the internal callback and the external callback when a domain is
  configured; `forgejo-manager` creates or updates the matching
  `YCluster` OAuth2 source using the internal discovery URL unless
  external mode is enabled.
- `templates/forgejo-internal.conf.j2` serves `forgejo.xc`;
  `files/forgejo_git.conf.j2` serves the external `git.<domain>` surface
  by default (`forgejo_external=true`), without Authentik forward-auth,
  because git clients and API tokens are not Authentik sessions.
- `app.py` adds `forgejo.xc` to the generated static DNS hosts so every
  cluster node resolves the internal name through the storage VIP. When a domain
  is configured it also adds `git.<domain>`, so external SSH clone URLs resolve
  to the storage leader internally instead of hairpinning through the frontend.
- `files/frpc.toml.j2` registers a raw TCP `forgejo-ssh` proxy on frontend
  port 2222. The proxy follows the leader-gated `frpc`; `install-frp-server.yml`
  opens the port in the frontend UFW by default, with
  `forgejo_external_ssh=false` as the opt-out.
- `.forgejo/workflows/ci.yml` mirrors the six GitLab jobs with a manual
  token checkout, so it does not need `actions/checkout` or Node in the job
  image.
- The repository-scoped runner is not installed yet. The workflow is ready,
  but it will stay queued until the runner playbook lands.
- The forge data currently has no off-cluster file backup; only the
  PostgreSQL metadata is in `pg_dumpall`. See
  `docs/operations/disaster-recovery.md` for the open gap.

## Motivation

The cluster currently uses an upstream GitLab SaaS instance for source
hosting, merge requests, and CI. Issues, milestones, releases, and the wiki
are not in active use there, so this is a git-ref cutover, not a metadata
migration. Self-hosting the forge would remove the external dependency, keep
repository data and CI secrets inside the cluster, and make the forge part of
the same Authentik-backed account model as the other cluster
applications.

The question is not "can the cluster run GitLab CE?" — it can. The question
is whether the operational cost is justified. GitLab's own installation
requirements put a single-node baseline at **8 vCPU / 16 GB**, with a
documented memory-constrained floor of **8 GB**, plus roughly **40 GB** for
the application node, storage for all repositories, and **5–12 GB** for
PostgreSQL. That is before a runner. For a small cluster whose forge is one
service among many, that is a large permanent allocation for a workload that
is mostly git, merge requests, and a short CI pipeline.

Forgejo's own project describes it as lightweight enough for "nearly every
machine", including a Raspberry Pi or a small cloud instance. It does not
publish a formal minimum, so the target allocation in this design is an
estimate to be validated by a soak test, not an upstream guarantee.

## Sources

- `ext/forgejo/` — Forgejo `forgejo` branch at
  `b389890bcdb4460cfc974d78f24fc1ab138eb8cf` (2026-10-21), release
  notes through v17.0.0. The README states the lightweight target and
  the feature set (issues, pull requests, wikis, projects, releases,
  package registry, organizations, OIDC, Actions). `custom/conf/app.example.ini`
  is the configuration surface used below.
- `ext/forgejo-docs/` — Forgejo documentation at
  `e77b6628af9cd9d45f30204898d68f55a191ccb5` (2026-10-20).
  - `docs/admin/installation/docker.md` documents the official container
    images and compose layout.
  - `docs/admin/installation/database-preparation.md` documents PostgreSQL
    >= 14 and the SQLite trade-off.
  - `docs/admin/setup/backup.md` documents the backup set and the
    requirement to stop Forgejo for a consistent backup.
  - `docs/admin/actions/` documents Forgejo Actions, runner registration,
    and the runner security model.
  - `docs/admin/setup/recommendations.md` recommends SQLite only for low
    to moderate activity and PostgreSQL/MySQL for busier instances.
- `ext/gitea/` — Gitea `main` at
  `a8e1057f5e75b0f8cf11191acfde161aa9d88bd2` (2026-10-21), latest
  stable v1.27.3. Used to confirm that Forgejo's core feature and
  footprint claims are inherited from the shared Gitea codebase.
- `ext/onedev/` — OneDev `main` at
  `05b502d1194c7b4ab681fb24a9ae39ceb27c7ea9` (2026-10-21), latest
  stable v16.9.5. The README claims all features on a **1 core / 2 GB**
  box for medium-sized projects, and documents built-in CI/CD, package
  registries, and code search.
- `ext/soft-serve/` — Soft Serve `main` at
  `6b169bc3af4d407aec43fb64c606d02af520bd0e` (2026-10-05), latest
  stable v0.12.3. The README documents a single binary, SSH TUI, LFS,
  and access control, with no web forge.
- GitLab installation requirements, fetched 2026-10-21:
  `https://docs.gitlab.com/install/requirements/`.

## Options considered

| Option | Version | Footprint | Fit |
|---|---|---|---|
| **GitLab CE** | 19.x | 8 vCPU / 16 GB baseline; 8 GB memory-constrained floor; 40 GB application, separate Gitaly storage, 5–12 GB PostgreSQL; runner separate | Full GitLab CI and review model. Rejected as too heavy for one cluster app. |
| **Forgejo** | 17.0.0 | No published minimum; target 2 vCPU / 2 GB for this deployment (estimate) | Chosen. Gitea-compatible core, GPLv3, community-governed, OIDC, Actions, optional package registry. |
| **Gitea** | 1.27.3 | Same codebase and roughly the same footprint as Forgejo | Viable fallback. Larger ecosystem, but single-vendor governance and no reason to prefer it for a new install. |
| **OneDev** | 16.9.5 | 1 core / 2 GB for medium-sized projects per upstream | Strong alternative if built-in CI matters more than GitHub-like workflow. Java, smaller ecosystem, different API and migration path. |
| **Soft Serve** | 0.12.3 | Single Go binary, negligible next to the other options | Not a GitLab replacement. No web forge, merge requests, issues, or CI. Only suitable if the requirement is a git remote with an SSH UI. |

### Why Forgejo over GitLab CE

Forgejo gives the parts the cluster actually uses — repositories,
pull requests, reviews, protected branches, webhooks, LFS, OIDC, and a
GitHub-Actions-like CI — at roughly an order of magnitude less memory
and CPU. It also fits the cluster's existing container, PostgreSQL,
Authentik, and RBD patterns.

GitLab CE is the right answer only if the cluster needs GitLab-specific
features: merge trains, approval rules, advanced CI semantics, or the
existing `.gitlab-ci.yml` unchanged. None of those are requirements in
this repo today. The current pipeline is six jobs and roughly 115 lines.

### Why Forgejo over Gitea

Forgejo and Gitea are the same core codebase, so the footprint and
feature comparison is close. Forgejo is chosen for governance: it is
GPLv3 and explicitly community-owned, while Gitea is a single-vendor
project. The migration path from Gitea to Forgejo is documented upstream,
so choosing Forgejo now does not create a one-way door.

### Why not OneDev

OneDev is the strongest alternative if the priority is "one process, built-in
CI, no separate runner". Its 1 core / 2 GB claim is credible, and its
built-in package registry and code search are attractive. It loses on
ecosystem and familiarity: its CI model is not GitHub Actions, its API is
different, and the cluster's existing GitLab usage and `tea`-style tooling
do not carry over. Forgejo is the conservative choice for a GitLab-shaped
workflow.

### Why not Soft Serve

Soft Serve is a very good minimal git server, but it is not a forge. It has
no web UI, issues, pull requests, reviews, or CI. It would only make sense
if the cluster wanted to replace the git remote and nothing else.

## Architecture

### Topology

```
Authentik (OIDC) ──┐
                   ├── forgejo.xc (nginx vhost, storage VIP)
PostgreSQL ────────┤        │
                   │        └── Forgejo (leader-gated compose stack)
/rbd/user/forgejo ─┘                │
                                    ├── repositories, LFS, attachments,
                                    │   avatars, Actions logs/artifacts,
                                    │   indexes, queues
                                    └── built-in SSH server :2222

compute node / isolated VM
  └── Forgejo Runner (repository-scoped, ephemeral, Docker/Podman)
```

### Runtime shape

- One Forgejo container plus its PostgreSQL database. No Redis, no
  Gitaly, no Sidekiq, no bundled nginx.
- Leader-gated on the storage leader, following Authentik and Zulip.
  Non-leaders do not run the stack.
- The web port is published on loopback only. Host nginx is the single
  ingress point, as with Open-WebUI.
- The built-in SSH server is enabled on port 2222. Internal clone URLs use
  `forgejo.xc:2222`; external SSH uses a leader-gated frp raw TCP proxy on
  the frontend's port 2222, gated by `forgejo_external_ssh`.
- State lives on `/rbd/user/forgejo`; the database lives in the existing
  PostgreSQL 16 instance. `pg_hba.conf` already permits
  `172.0.0.0/8` with `scram-sha-256`.

### Sizing

Target allocation: **2 vCPU / 2 GB RAM / 20 GB** for the forge container,
plus database growth. This is deliberately below GitLab CE's 8 GB floor and
within the range Forgejo describes as a small cloud instance. The soak test
should record idle and peak RSS, CI concurrency, and repository index size.
If the instance stays under 2 GB with the expected repository count, keep it;
if not, move to 4 GB before considering a different forge.

PostgreSQL is chosen over SQLite because the forge has concurrent web, git,
and CI traffic, and because the cluster already runs PostgreSQL with a
leader/failover story. SQLite remains a valid fallback for a single-user
instance, but it makes the backup and concurrency story worse here.

### Availability and failover

Forgejo is stateful and single-instance in this design. It follows the
storage leader: PostgreSQL and `/rbd/user` move together, and the compose
stack restarts on the new leader. This is not active-active and does not
attempt a multi-instance Forgejo topology. The acceptance test is a leader
failover with a clone, a push, and a login during the window.

## Authentication

### OIDC through Authentik

- Create an Authentik OAuth2/OpenID provider for Forgejo and a matching
  application. The provider always includes the internal callback
  `http://forgejo.xc/user/oauth2/YCluster/callback` and adds the external
  callback `https://git.<domain>/user/oauth2/YCluster/callback` when a
  domain is configured.
- Configure Forgejo's OAuth2 source with the Authentik discovery URL,
  client ID, and client secret. The source is named `YCluster`, so the login
  button reads **Sign in with YCluster**. Discovery always uses the public
  IdP hostname `https://auth.<domain>`; `auth.xc` is not an SSO surface.
  Internal clients resolve `auth.<domain>` to the storage VIP through the
  generated hosts file. Request `openid profile email`. The provider grants
  `authorization_code` and `refresh_token`.
- Publish the `Forgejo` application in the Authentik app library and on the
  root `<domain>` landing page.
- Set `ENABLE_AUTO_REGISTRATION = true` so the first Authentik login
  creates the Forgejo account.
- Give the provider its own `profile` scope mapping and set
  `USERNAME = nickname`. Authentik usernames are email addresses, and
  Forgejo rejects `@` in account names. The mapping replaces characters
  Forgejo does not allow with `-`, collapses runs, trims the ends, caps the
  name at 40 characters, and prefixes `user-` if the result would not start
  with a letter or digit. `alice@example.com` becomes
  `alice-example.com`, so two users on different domains do not collide.
  A user's `forgejo_username` attribute overrides the derived name for a
  residual collision or a preferred spelling.
- Set `allow-username-change` on the OAuth2 source so OIDC users can
  rename themselves under **Settings → Account**. The initial name still
  comes from the claim; the external login binding is by `sub`, so a rename
  does not break login. No rename cooldown is configured.
- Set `ACCOUNT_LINKING = auto` only because Authentik is the cluster's
  authoritative IdP and its email claim is verified. If that assumption
  stops holding, switch to `login` and require a one-time link.
- Set `DISABLE_REGISTRATION = true`. There is no open signup.
- Keep `ENABLE_INTERNAL_SIGNIN = true` for the single local
  break-glass administrator. The local account is not a normal user path.

The break-glass account is `forgejo-admin`. Its generated password is
stored in etcd at `/cluster/config/forgejo/admin-password`. It is not in
`app.ini` and is not committed to the repo; retrieve it with the etcd
client environment from a core node.

The Authentik manager owns the provider and client secret, following the
Open-WebUI pattern. The Forgejo manager reads the secret from etcd and
writes it into Forgejo's OAuth2 source in the database, not `app.ini`;
the secret is never committed to the repo. `app.ini` holds only Forgejo's
own generated secrets and is mode `0600`.

### Git authentication

Users access git with a normal git client:

```bash
# HTTPS: username + a Forgejo access token as the password
git clone http://forgejo.xc/<owner>/<repo>.git

# SSH: a key registered in Forgejo (internal)
git clone ssh://git@forgejo.xc:2222/<owner>/<repo>.git

# SSH from outside the cluster, when external SSH is enabled
git clone ssh://git@git.<domain>:2222/<owner>/<repo>.git
```

- HTTPS git uses Forgejo access tokens, not account passwords. Create one
  under **Settings → Applications → Generate token** with repository
  read/write scope. Use the Forgejo username and that token when git prompts.
- SSH uses Forgejo's built-in SSH server on port 2222. Users add their public
  key under **Settings → SSH / GPG Keys**; the private key stays local.
- With `forgejo_external_ssh=true`, `SSH_DOMAIN` becomes `git.<domain>` so
  the clone button advertises the external endpoint. Internal clients resolve
  that name to the storage VIP through the generated hosts file; external
  clients use the frontend's frp tunnel.
- Existing SSH keys are user-managed in Forgejo. The cutover does not
  carry GitLab SSH keys; users re-add them or use a token.

## CI with Forgejo Actions

Forgejo does not run jobs itself. It hands them to a Forgejo Runner, which
executes arbitrary code by design. The runner is therefore the highest-risk
part of this design.

### Runner placement and scope

- Run the runner on a compute node or a dedicated VM, never on the storage
  leader and never next to PostgreSQL.
- Register it at repository scope for the `ycluster` repository first. Widen
  to organization scope only if a second repository needs it.
- Use ephemeral mode where possible. A leaked runner token should not be
  able to pick up another job.
- Use container jobs (`docker` or `podman` labels), not `host` execution.
- Keep `container.privileged = false`, leave `container.network` at the
  isolated-per-job default, keep `valid_volumes = []`, set a container
  memory/CPU limit, and keep `runner.capacity = 1` until the node is sized.
- Give the runner no credentials to the cluster registry beyond what it needs
  to pull its job images. A workflow author can specify arbitrary images, so
  registry credentials are part of the trust boundary.

### Workflow migration

The current `.gitlab-ci.yml` has six real jobs: Python lint, Jinja lint,
Ansible lint, Go lint, Python tests, and Go tests. They map to a single
`.forgejo/workflows/ci.yml` with GitHub-Actions-like syntax:

- `on: [push, pull_request]`
- `runs-on: docker`
- manual token checkout first (`git init` + `git fetch` with
  `FORGEJO_TOKEN`), so no `actions/checkout` and no Node.js requirement
- `run:` steps for the existing commands
- `python:3.13` and `golang:1.25` job images, mirrored into `registry.xc`

The manual checkout avoids both the external action dependency and the
Node.js requirement that `actions/checkout` would impose on a slim Python
image. The tradeoff is that the same checkout snippet is repeated in each
job.

Forgejo Actions is GitHub-Actions-like but not compatible. The migration
must be validated by running both pipelines on the same commit and comparing
results, not by assuming the YAML translates.

### Action and image supply chain

The current workflow uses no `uses:` actions, so it has no remote-action
supply chain. Job images are still pulled from public registries, which is an
external dependency and a supply-chain surface.

- Set `DEFAULT_ACTIONS_URL` to the internal forge, and mirror any future
  action repositories into it before use.
- Pin any future actions by commit SHA.
- Mirror job images into `registry.xc` and reference them by digest.
- If the runner must be fully offline, pre-seed every image and keep using
  `run:` steps only.

This is more work than the GitLab pipeline, but it is bounded: the current
pipeline needs two language images and the existing test commands.

## Cutover from the existing GitLab remote

The current GitLab use is a git remote plus merge requests and CI. Issues,
milestones, releases, wiki, and package registry are not in active use, so
there is no metadata import. CI variables are recreated explicitly if any
exist. This is a ref cutover.

### Cutover sequence

1. Create the internal Forgejo instance and the OIDC source. Verify login
   with a non-admin Authentik account and with the local break-glass admin.
2. Add Forgejo as a second remote and push all branches and tags. Compare
   refs and a sample of commit SHAs. No issue or merge-request metadata is
   imported; land or close any open merge requests before cutover.
3. Add the Forgejo Actions workflow to the repository. Keep the GitLab
   pipeline running on the same commit until the results match.
4. Make the old GitLab project read-only. Soak for a short period with both
   remotes available.
5. Change `origin` to Forgejo, re-add SSH keys and tokens, and update
   local tooling. Keep the old GitLab project read-only as an archive until
   the soak ends.
6. Remove the old GitLab remote only after the archive decision is explicit.

### Tooling

`python-gitlab`, the `gitlab` CLI wrapper, and the current `gitlab`
skill do not apply to Forgejo. The replacement is `tea` (Gitea/Forgejo CLI)
or the Forgejo API. The existing MR workflow can be preserved, but the
commands and configuration change.

## Operations

### Backup and restore

Forgejo's backup set is not just the git repositories:

- PostgreSQL database `forgejo`.
- `/rbd/user/forgejo` (repositories, LFS, attachments, avatars,
  Actions logs/artifacts, indexes, queues).
- `app.ini`, including `SECRET_KEY`, `INTERNAL_TOKEN`, `JWT_SECRET`, and
  `LFS_JWT_SECRET`, plus the SSH host key files referenced by
  `SSH_SERVER_HOST_KEYS`.
- The generated PostgreSQL password in etcd at
  `/cluster/config/forgejo/db-password`, covered by the etcd backup.
- The generated break-glass admin password in etcd at
  `/cluster/config/forgejo/admin-password`, covered by the etcd backup.
- The Authentik OIDC client secret, in the Authentik backup.

Forgejo's docs strongly recommend shutting down the instance for a
consistent backup. Do not treat a live rsync of the data directory as a
backup. A `forgejo dump` is useful as a secondary copy, not as the only
backup.

The restore drill is part of acceptance: restore the database and data
directory into a scratch instance, log in through OIDC, clone a repository,
and run the CI workflow.

### Upgrades

Forgejo publishes a stable release every three months and patch releases
more frequently. Patch and minor upgrades are container replacement plus
automatic migrations. Major upgrades require a backup and a restore plan.
Pin the image by digest; do not deploy a floating `17` tag.

### Monitoring

At minimum, monitor:

- HTTP health and login latency.
- PostgreSQL connections and slow queries.
- Container RSS and CPU.
- Runner queue depth and failed jobs.
- Disk usage for repositories, LFS, Actions logs, and artifacts.
- Backup age and restore-drill age.

## Security

- External HTTPS and SSH are enabled by default. Disable either surface
  with `-e forgejo_external=false` or `-e forgejo_external_ssh=false`.
- The web surface goes behind the existing nginx/certbot/frp path. Do not
  publish the Forgejo container port directly.
- External SSH is the exception to the nginx path: it is a raw TCP frp proxy
  on port 2222, gated separately by `forgejo_external_ssh`. It does not
  change the frontend's own sshd on port 22.
- Do **not** put `git.<domain>` behind Authentik forward-auth. Git clients
  and API tokens are not Authentik sessions; Forgejo's token auth and OIDC
  login are the boundary. This is the deliberate exception to the usual
  public-vhost checklist.
- The runner is remote code execution. Isolate it on a compute node or VM,
  restrict its network reachability, and do not give it storage-leader
  credentials or the Docker socket of a shared host.
- Do not enable `container.privileged`, host networking, or `valid_volumes`
  for the runner.
- Keep the local break-glass admin, but do not make it the normal login
  path. If Authentik is down, the forge still has an admin path; if the
  forge is down, the cluster's other apps are unaffected.
- Back up `SECRET_KEY` and `JWT_SECRET`. Losing them breaks stored
  credentials, Actions secrets, webhook credentials, and OIDC sessions.
- Pin the Forgejo image by digest and track it with `osv-feed`, as with
  Authentik. Do not deploy a floating tag.

## Acceptance criteria

- `forgejo.xc` serves the web UI over HTTP inside the cluster;
  `git.<domain>` serves it over HTTPS by default (`forgejo_external=true`).
- Authentik OIDC login works for a normal user; local admin login works
  as break-glass.
- HTTPS and SSH clone/push work with a token and a user key.
- With `forgejo_external_ssh=true` (default), SSH clone/push works from
  outside the cluster through `ssh://git@git.<domain>:2222/...`; the
  frontend's port 22 is unchanged.
- All branches and tags from the old remote are present with matching SHAs.
- The Forgejo Actions workflow passes the same six checks as the GitLab
  pipeline on the same commit.
- A leader failover preserves login, clone, push, and CI.
- A backup/restore drill succeeds on a scratch instance.
- Idle and peak RSS stay within the target allocation; record the actual
  numbers in the implementation MR.
- No core git or web operation depends on an external SaaS service.

## Non-goals

- GitLab CI compatibility. The pipeline is rewritten.
- Public open registration.
- Forgejo federation (ActivityPub) as a requirement.
- Issue, milestone, release, or wiki migration; those are not in use.
- Migrating GitLab package registry contents, CI variables, or runners.
- Active-active Forgejo HA.
- Replacing the cluster's existing container registry with Forgejo's
  package registry.

## Open questions

- Is external `git.<domain>` required, or is VPN/internal access
  sufficient? This determines whether external SSH is a new public TCP
  surface.
- Is a repository-scoped runner enough, or will multiple repositories need
  an organization-scoped runner?
- Does the forge need to remain usable when Authentik is down for normal
  users, or is a local break-glass admin sufficient?
- Should the GitLab project remain as a read-only archive indefinitely, or
  be removed after the soak?

## Implementation order

1. Add `install-forgejo.yml` plus `forgejo-manager` and compose files:
   PostgreSQL database/role, `/rbd/user/forgejo`, digest-pinned image,
   `app.ini`, nginx vhost, and backup hooks.
2. Add the Authentik OIDC provider and the Forgejo OAuth2 source.
   Verify normal-user login, break-glass login, and account linking.
3. Seed `ycluster` from the existing refs, add the Forgejo remote, and
   run a clone/push smoke test.
4. Add the repository-scoped runner on a compute node and rewrite
   `.gitlab-ci.yml` as `.forgejo/workflows/ci.yml`. Run both pipelines
   on the same commit.
5. Soak, then cut over `origin`, make GitLab read-only, and record the
   backup/restore and failover results.
6. Decide external exposure separately. If approved, add `git.<domain>`
   through the existing edge path and document the SSH decision.
