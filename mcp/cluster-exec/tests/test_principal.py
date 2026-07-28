"""Test `_principal_from_ctx` picks a stable caller identifier.

Preference order: `params._meta.threadId` (stable across reconnects
within a client process — what codex sends) over the `mcp-session-id`
header (transport-level, can rotate on reconnect). Returns `None` if
neither is present. These tests exercise the extraction helper directly.
"""

import unittest

from starlette.requests import Request

from cluster_exec import server


def _make_ctx(headers: dict[str, str], *, thread_id: str | None = None):
    """Build a fake Context carrying a starlette Request with the given
    headers and (optionally) a `_meta` object with `threadId`.

    `_principal_from_ctx` reads `ctx.request_context.request.headers` and
    `ctx.request_context.meta`, so we plumb those two attributes through
    with duck-typed objects. Other Context fields are irrelevant.
    """
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    request = Request(scope)
    # Minimal ServerRequestContext-like object: the helper only touches
    # `.request` and `.meta`, so duck-typed attributes suffice.
    meta: dict | None = {"threadId": thread_id} if thread_id else None
    class _RC:
        request: Request | None
        meta: dict | None
    rc = _RC()
    rc.request = request
    rc.meta = meta
    from mcp.server.mcpserver.context import Context
    return Context(request_context=rc)


class PrincipalFromCtxTests(unittest.TestCase):
    def test_returns_thread_id_when_meta_present(self):
        ctx = _make_ctx({}, thread_id="019fa932-f94d-79b0-8c7a-7884da403c39")
        self.assertEqual(
            server._principal_from_ctx(ctx),
            "019fa932-f94d-79b0-8c7a-7884da403c39",
        )

    def test_thread_id_takes_precedence_over_session_id(self):
        # Both signals present — the threadId wins because it survives
        # transport reconnects, whereas mcp-session-id can rotate.
        ctx = _make_ctx(
            {"mcp-session-id": "abc123def456"},
            thread_id="019fa932-f94d-79b0-8c7a-7884da403c39",
        )
        self.assertEqual(
            server._principal_from_ctx(ctx),
            "019fa932-f94d-79b0-8c7a-7884da403c39",
        )

    def test_falls_back_to_session_id_when_meta_missing(self):
        # Client doesn't populate `_meta.threadId` (e.g. opencode) — we
        # fall back to the transport-level header.
        ctx = _make_ctx({"mcp-session-id": "abc123def456"})
        self.assertEqual(server._principal_from_ctx(ctx), "abc123def456")

    def test_falls_back_to_session_id_when_meta_is_empty(self):
        # Client sends `_meta` but without `threadId`.
        ctx = _make_ctx({"mcp-session-id": "abc123def456"})
        # Manually override meta to empty dict (no threadId).
        ctx._request_context.meta = {}
        self.assertEqual(server._principal_from_ctx(ctx), "abc123def456")

    def test_returns_none_when_both_missing(self):
        ctx = _make_ctx({})
        self.assertIsNone(server._principal_from_ctx(ctx))

    def test_empty_thread_id_falls_back_to_session_id(self):
        # Defensive: a `threadId` of empty string shouldn't pass the
        # truthiness check and should fall through to the header.
        ctx = _make_ctx({"mcp-session-id": "abc123def456"}, thread_id="")
        self.assertEqual(server._principal_from_ctx(ctx), "abc123def456")

    def test_returns_none_when_request_is_none(self):
        # A Context whose request_context.request is None (stateless
        # transport or test scaffolding) must not blow up.
        from mcp.server.mcpserver.context import Context
        class _RC:
            request: Request | None = None
            meta: dict | None = None
        ctx = Context(request_context=_RC())
        self.assertIsNone(server._principal_from_ctx(ctx))

    def test_returns_none_when_request_context_missing(self):
        # A bare Context with no request_context (e.g. invoked outside a
        # tool handler) must not blow up.
        from mcp.server.mcpserver.context import Context
        ctx = Context()
        self.assertIsNone(server._principal_from_ctx(ctx))

    def test_thread_id_is_stringified(self):
        # MCP spec types `threadId` as a string, but the helper should
        # coerce defensively if a client sent something else.
        class _RC:
            request: Request | None = None
            meta: dict | None = None
        _RC.meta = {"threadId": 12345}  # type: ignore[attr-defined]
        from mcp.server.mcpserver.context import Context
        ctx = Context(request_context=_RC())
        self.assertEqual(server._principal_from_ctx(ctx), "12345")


if __name__ == "__main__":
    unittest.main()
