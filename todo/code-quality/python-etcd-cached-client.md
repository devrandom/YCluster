# Python: global etcd client cached forever

From `docs/reviews/2026-06-09-codebase-review.md` Minor section.
`get_etcd_client()` (`common/etcd_utils.py:86`) returns a process-wide
`_CACHED_CLIENT` that goes stale after member changes.

Same root cause as `../operations/admin-api-etcd-failover.md` (the
operational impact entry — keep this entry focused on the code-level
fix).

## Related Minor items

- Mixed `print(file=sys.stderr)` vs logging in `app.py`
- MAC normalization duplicated across ~5 files
- nginx `-t` stderr discarded in `certbot_manager.py:215`
