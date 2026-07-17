# H3 — systemd units lack hardening directives

From `docs/reviews/2026-06-09-codebase-review.md`. systemd units for the
DHCP server and admin-api have no hardening directives even where root
is required for raw sockets.
