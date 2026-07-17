# install-mineru.yml rebuild detection

Today the `Check if mineru image is already in registry` task HEADs the
registry tag and skips rebuild if present. That misses Containerfile /
UDS wrapper / Containerfile changes — the first redeploy shipped the new
entrypoint only after we manually deleted the tag, because the registry
still had the old digest under the same tag.

Switch to digest comparison: after build, compare
`buildah images --format '{{.Digest}}'` (root storage) vs.
`skopeo inspect --raw docker://registry.xc:5000/mineru:TAG`
→ `Digest` header; different → rebuild flag, same → skip.

Note that the new image still pushes under the same tag (operators don't
see a version bump), and the systemd unit's `ExecStartPre=podman pull`
will pick up the new digest on next start — no manual tag gymnastics
required for routine changes.

Refs:
- `../security/sandbox-mineru-outbound.md` (the redeploy that exposed this gap)
