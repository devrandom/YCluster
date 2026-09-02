# Python: reduce health and CLI test over-mocking

The Python suites are fast, but the health and CLI suites lean heavily on
patches around private module state. Many tests assert mock call sequences
rather than observable behavior, which makes them brittle under
behavior-preserving refactors.

## Health modules

- Extract shared health status and probe logic duplicated between `app.py`
  and the lite health API modules, especially Tang helpers and
  `_add_health_service`-style rules.
- Introduce a small probe/context seam for node role, hostname, etcd client,
  service status, port checks, and HTTP probes.
- Rework health tests to provide fake probe context data and assert the
  resulting health payload instead of patching `_IS_ETCD_NODE`,
  `_HOSTNAME`, `_get_etcd_client`, and helper call ordering.
- Reserve call/caching assertions for seams where ordering or caching is the
  contract.

## CLI wrappers

- Keep parser-registration and dispatch coverage, but remove exhaustive tests
  that only prove one thin wrapper forwards to a manager function.
- Preserve focused behavior tests for argument normalization, error paths,
  and exit codes.

## Acceptance

- `just test` and `just metrics` pass from an agent worktree.
- Duplicate health logic is removed or unified at a source level.
- Tests exercise observable health status and CLI behavior rather than
  private implementation order.
- No broad mechanical rewrite: reduce verbosity where it improves the test's
  behavioral value without weakening coverage.
