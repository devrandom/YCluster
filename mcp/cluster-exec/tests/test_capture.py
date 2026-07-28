"""Test the redaction helpers used by the debug-only request capture.

`CE_CAPTURE_REQUESTS=<path>` writes a JSONL log of every incoming
HTTP request. The bearer token and project-specific fields (git
remote URLs, commit hashes) must not leak to disk — the helpers
below redact them before write. These tests cover the helper
functions directly so we don't need to spin up a starlette app.
"""

import json
import unittest

from cluster_exec import server


class RedactAuthorizationTests(unittest.TestCase):
    def test_replaces_authorization_with_bearer_mask(self):
        headers = [
            ("authorization", "Bearer haM6x2I7YR9VdyV5A70NnyatWMk-9hjzfZIgQE_w7gY"),
            ("mcp-session-id", "abc123"),
            ("accept", "application/json"),
        ]
        result = server._redact_authorization(headers)
        self.assertEqual(result[0], ("authorization", "Bearer ***"))
        self.assertEqual(result[1], ("mcp-session-id", "abc123"))
        self.assertEqual(result[2], ("accept", "application/json"))

    def test_case_insensitive_header_match(self):
        # Starlette yields original-case names; some clients / proxies
        # might capitalise differently. We match case-insensitively.
        headers = [("Authorization", "Bearer SECRET")]
        result = server._redact_authorization(headers)
        self.assertEqual(result[0], ("Authorization", "Bearer ***"))

    def test_leaves_non_sensitive_headers_unchanged(self):
        headers = [
            ("mcp-session-id", "abc"),
            ("content-type", "application/json"),
            ("mcp-protocol-version", "2025-06-18"),
        ]
        result = server._redact_authorization(headers)
        self.assertEqual(result, headers)

    def test_empty_input(self):
        self.assertEqual(server._redact_authorization([]), [])


class ScrubGitRemoteTests(unittest.TestCase):
    def test_scp_form(self):
        # user@host:path -> host
        self.assertEqual(
            server._scrub_git_remote("git@gitlab-p:devrandom01/ycluster.git"),
            "gitlab-p",
        )

    def test_https_form(self):
        self.assertEqual(
            server._scrub_git_remote("https://github.com/foo/bar.git"),
            "github.com",
        )

    def test_ssh_form(self):
        self.assertEqual(
            server._scrub_git_remote("ssh://user@gitlab-p/devrandom01/ycluster.git"),
            "gitlab-p",
        )

    def test_non_string_passes_through(self):
        # Defensive — should never happen on the wire, but the helper
        # must not crash if it does.
        self.assertEqual(server._scrub_git_remote(None), None)
        self.assertEqual(server._scrub_git_remote(42), 42)


class RedactBodyTextTests(unittest.TestCase):
    def test_redacts_git_remote_in_codex_turn_metadata(self):
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "_meta": {
                    "x-codex-turn-metadata": {
                        "session_id": "019fa932-f94d-79b0-8c7a-7884da403c39",
                        "workspaces": {
                            "/home/user/proj/ycluster": {
                                "associated_remote_urls": {
                                    "origin": "git@gitlab-p:devrandom01/ycluster.git",
                                },
                                "latest_git_commit_hash": "8d12c14335af16fc21312f8736bc33b913d513e1",
                                "has_changes": True,
                            },
                        },
                    },
                },
                "name": "run",
                "arguments": {"command": "echo true", "hostname": "localhost"},
            },
        })
        redacted = server._redact_body_text(body)
        parsed = json.loads(redacted)
        ws = parsed["params"]["_meta"]["x-codex-turn-metadata"]["workspaces"]["/home/user/proj/ycluster"]
        self.assertEqual(
            ws["associated_remote_urls"]["origin"],
            "gitlab-p",
        )
        self.assertEqual(ws["latest_git_commit_hash"], "***")
        # Non-sensitive fields pass through untouched.
        self.assertEqual(ws["has_changes"], True)
        self.assertEqual(parsed["params"]["arguments"]["command"], "echo true")
        self.assertEqual(
            parsed["params"]["_meta"]["x-codex-turn-metadata"]["session_id"],
            "019fa932-f94d-79b0-8c7a-7884da403c39",
        )

    def test_non_json_body_passes_through(self):
        # Plain GET requests have no body — should be a no-op.
        self.assertEqual(server._redact_body_text(""), "")
        self.assertEqual(
            server._redact_body_text("not json at all"),
            "not json at all",
        )

    def test_body_without_codex_metadata_passes_through(self):
        # An initialize handshake or notifications/initialized doesn't
        # have `x-codex-turn-metadata`. Should round-trip unchanged.
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        })
        self.assertEqual(server._redact_body_text(body), body)

    def test_body_with_unexpected_workspace_shape_does_not_crash(self):
        # Defensive: a client might send a workspace dict missing
        # `associated_remote_urls`, or with non-string values.
        body = json.dumps({
            "params": {"_meta": {"x-codex-turn-metadata": {
                "workspaces": {
                    "/some/path": {
                        "associated_remote_urls": {},
                        "latest_git_commit_hash": "deadbeef",
                    },
                    "/other/path": "not a dict",
                },
            }}},
        })
        parsed = json.loads(server._redact_body_text(body))
        ws = parsed["params"]["_meta"]["x-codex-turn-metadata"]["workspaces"]
        self.assertEqual(ws["/some/path"]["latest_git_commit_hash"], "***")
        self.assertEqual(ws["/other/path"], "not a dict")


if __name__ == "__main__":
    unittest.main()
