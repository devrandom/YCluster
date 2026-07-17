# A2 — API keys plaintext

From `docs/reviews/2026-06-09-codebase-review.md`. The etcd master key
and Open-WebUI `api_key` rows are stored plaintext. Threat-model item;
hashing the OWUI keys needs upstream changes.
