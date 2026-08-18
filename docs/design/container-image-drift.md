# Container Image Scan Drift

## Why

`container-image-tracking.md` makes the CVE posture for the public app images
(authentik, open-webui, zulip) a function of the **manifest** the
`osv-feed` collector scans (`/near1/osv-feed/container/*.manifest`), not of
what actually runs. That leaves the accuracy of the CVE alerts dependent on a
manual step: `install-osv-feed.yml` re-syncs `container-manifests/` into the
shared snapshot dir, and only when it is run.

A real incident showed the failure mode: `authentik` was
bumped to 2026.5.6 (digest `sha256:5a77d345`) and deployed, but the snapshot
still held the 2026.2.4 manifest (`pyopenssl 25.3.0`, `cryptography 46.0.5`).
The collector kept scanning the stale manifest, so
`PerimeterCVECriticalLongPending` fired two false positives about a version
that was not running. Nothing detected that the scan input had silently
diverged from the running images.

## Invariant

The scan is faithful to the running system iff:

> for every tracked image, the **identity of the container actually running**
> equals the **identity recorded in the manifest the scanner reads**.

If yes, the CVE info describes the running surface. If no, the scan is lying
in one direction or the other:

- snapshot behind running (the incident): scan claims old vulnerable versions
  while the new image runs — false positives,
- snapshot ahead of running (manifest bumped, old image still deployed): scan
  claims `patched` while the vulnerable image runs — false negatives.

Both are the same detection. We bind the identity, not the contents.

This deliberately does **not** attempt to also verify "is the snapshot the
latest version the repo declares". Being behind on version while running a
consistent image is an upgrade-cadence concern, not a scan-accuracy one, and
is out of scope. Comparing against a second copy of the declared manifests
(the repo / bastion `/etc/ansible` copy) pulls a third source of truth in and
turns the check into a sync-freshness alarm that can itself go stale; it adds
machinery without tightening the run-vs-scan invariant.

## Identity token

Both sides must expose a comparable token, but the mechanism must not care
*which* images exist or how each one's token is derived. So instead of a
hardcoded per-image table in the collector, the manifest carries its own
identity directive and the collector discovers images by reading manifests.
Adding an image is then data, not code.

The manifest is the single place that declares an image's name and its
expected identity. We add one machine-readable directive line to the existing
`osv-feed` parser and to `manifestgen`, so every manifest becomes
self-describing:

```
@image <name> version="..." digest="..."
```

- The image name is derived from the manifest file name (already how
  `osv-feed` labels container findings via `image_name()`), so there is no
  separate registry of names and the directive's `<name>` must match it.
- `digest` is populated when the build produces a pinned digest (authentik:
  `ghcr.io/goauthentik/server@sha256:5a77d345`).
- `version` is populated from provenance stamped at build time for the
  in-cluster builds (`org.opencontainers.image.version` / `.revision` in
  `build-open-webui.yml` / `build-zulip.yml`).

Today only authentik already has a comparable token (its pinned digest). The
two in-cluster builds need the OCI labels stamped once — that is the single
unavoidable build-side change, and it is small and uniform.

### Manifest plumbing

Two one-time changes to `osv-feed` make the directive a first-class citizen
without touching how spec lines (`PyPI:name=version`, ...) are matched:

- `osv-feed/osv_feed/container.py` learns to parse zero or one `@image`
  directive per manifest, alongside the existing strict
  `ecosystem:name=version` spec lines (which are unchanged, so CVE matching is
  unaffected). A manifest without a directive parses fine but is flagged
  "unverifiable" by the collector rather than silently treated as in sync.
- `osv-feed/osv_feed/manifestgen.py` `generate()` emits the directive whenever
  the caller supplies the identity, so it rides along when manifests are
  regenerated from lockfiles.

Like today's manifest generation, this is **offline and committed, not a
deploy-time step**: a dev runs `manifestgen` at pin time to produce
`config/ansible/monitoring/container-manifests/<name>.manifest`, the result is
checked in, and `install-osv-feed.yml` rsyncs those committed files into the
shared snapshot dir (`/near1/osv-feed/container/`, `delete: yes`) on deploy.
So the `@image` directive is authored into the committed manifest alongside
the spec lines and travels to the snapshot unchanged — nothing regenerates it
on the node, and the collector reads the same committed artifact it matches
CVE findings from.

## Genericity: adding an image is data, not code

Adding a tracked image never touches the collector, the parser, the alert
rule, or any per-image branch:

1. Add `container-manifests/<name>.manifest` with an `@image` directive whose
   identity matches what the build stamps.
2. Build / deploy the image so its OCI labels or pinned digest encode the same
   identity.
3. The manifest rides through `install-osv-feed.yml` into the shared snapshot
   like every other manifest.

The collector has no image list: it iterates every `*.manifest`, reads that
file's `@image` directive, `docker inspect`s the same-named running
container, and compares. There is no branch in the collector or the alert that
names an image.

Opt-in / unverifiable states are explicit rather than silent:

- **No manifest** for an image → not scanned at all (today's comfyui state;
  nothing to compare, no alert).
- **Manifest present but no `@image` directive** → the scanner cannot bind the
  run to the scan, so the identity is unverifiable. Rather than quietly
  passing, the collector emits `perimeter_image_scan_drift{...} 1` with an
  `unverifiable` dimension so an old hand-written manifest can't look "in
  sync" just because no directive was ever added.
- **Directive names a container that isn't running** → drift (the declared
  surface isn't deployed).
- **Running image missing the OCI label / digest** → drift; a build that
  forgot to stamp provenance is a real divergence, not a pass.

## Collector

A single **storage-leader-scoped** textfile collector (the leader is a
storage node, so it mounts `/near1` *and* runs all the containers — no
cross-node hop). It runs on the leader like `install-authentik.yml` already
detects leader-ness, and writes `perimeter_image_scan_drift.prom`:

- list `/near1/osv-feed/container/*.manifest` (no hardcoded image list),
- for each manifest, parse its `@image` directive → expected identity and the
  image name it applies to,
- `docker inspect` the same-named container → actual identity (`RepoDigests`
  for a pinned digest, `org.opencontainers.image.*` labels otherwise),
- compare and emit one line set per image:

```
perimeter_image_scan_drift{image="authentik"} 0
perimeter_image_scan_drift{image="open-webui"} 1
perimeter_image_scan_drift_expected{image="open-webui"} "open-webui-v0.11.0-f9590b80"
perimeter_image_scan_drift_actual{image="open-webui"} "open-webui-v0.12.0-abcdef01"
```

The leader mounting `/near1` means the snapshot is read in place and cannot
itself be the source of a stale snapshot on this host — any snapshot staleness
shows up as a mismatch against the running image. During a leader shift both
leaders emit partial data briefly; the alerts are short-lived, matching the
existing pattern in `docs/operations/`.

## Alert

Add to `config/ansible/monitoring/templates/ycluster-alerts.yml.j2` a
`PerimeterImageOutdated` rule (the alert name already reserved by
`container-image-tracking.md` P4), firing when
`perimeter_image_scan_drift == 1`:

- `severity: critical` — the perimeter CVE posture is untrustworthy.
- `for:` a short window so a leader-shift blip doesn't page.
- description and labels are fully generic — they reference
  `{{ $labels.image }}`, `{{ $labels.expected }}` and `{{ $labels.actual }}`
  only. The collector labels each fired metric with `expected`/`actual`
  (and `unverifiable="true"` when no directive or no running container could
  be bound), so the operator sees which image drifted and in which direction
  without the rule hardcoding image names.

This is deliberately separate from `PerimeterCVECriticalLongPending` /
`PerimeterCVEHighLongPending`: CVE arrows say "your *declared* surface has a
vuln"; this arrow says "declared ≠ actual, so those CVE arrows are
unreliable". Keeping them independent means a drift condition is not silently
lost while CVE alerts clear or lag.

## Out of scope

- **comfyui** — no manifest is generated (untracked); nothing to compare, and
  it stays that way until someone opts it in by adding a manifest.
- **Version cadence** — running a consistent but older image than latest is not
  drift; that is the normal update flow already covered by the image
  build/deploy playbooks.
- **Non-PyPI layers** — the manifest only covers the uv/PyPI runtime surface;
  the identity comparison bounds accuracy of that surface, not of base OS or
  Node layers.

## Verification

- Unit tests for the identity-compare + directive-parse pure functions in
  `osv-feed/`, including: manifest with no directive (unverifiable), directive
  naming a non-running container (drift), and both directions of a value
  mismatch (mirroring `tests/test_container.py` / `tests/test_manifestgen.py`).
- `promtool check rules` on the new alert group.
- Dev-cluster fixtures: (1) a stale snapshot manifest vs running image →
  drift fires in the "snapshot behind" direction; (2) a bumped manifest before
  redeploy → drift fires in the "snapshot ahead" direction; then converge and
  confirm both clear. Add a third fixture that verifies a newly added image +
  manifest yields the same metrics shape with zero collector changes.
- Cross-reference this doc's incident in `todo/security/cve-tracking.md`
  close-out once wired.

## References

- `docs/design/container-image-tracking.md` — manifest-driven tracking; this
  doc lands the P4 `PerimeterImageOutdated` drift metric.
- `docs/design/osv-feed.md` — matching pipeline, `container` category, ack.
- `docs/design/perimeter-software-tracking.md` — perimeter inventory and tiers.
- `docs/operations/packages.md` — runbook / update flow.
