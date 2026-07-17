# H4 — docker-registry user UID collision risk

From `docs/reviews/2026-06-09-codebase-review.md`.
`storage/install-docker-registry.yml:36` pins the `registry` user to
UID 1002, in the sequential 1000+ range normally reserved for human
accounts. Vendor preinstall accounts routinely squat 1000–1002 and a
future node landing a human user there collides (the user is
`system: yes` but the numeric UID ignores that convention).

## Fix

Move it out of the 1000+ range (e.g. a dedicated value like `9001`, or
a wider service range like `8000–8099`) and update the matching
`user: "1002:1002"` reference at `install-docker-registry.yml:96`.
Sweep for other 1002 references (RBD ownership, image pulls) before
changing.
