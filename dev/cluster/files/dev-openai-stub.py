#!/usr/bin/env python3
"""Dev stand-in for an inference backend (vLLM/llama-server).

Serves just enough of the OpenAI surface for the system test to drive
local-ai-proxy end-to-end: GET /v1/models lists one model, POST
/v1/chat/completions returns a fixed completion that echoes the model
name, so a response proves which backend config served the request.
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

MODEL = "dev-echo"


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/models":
            self._send({"object": "list",
                        "data": [{"id": MODEL, "object": "model",
                                  "owned_by": "dev-stub"}]})
        else:
            self._send({"error": f"unknown path {self.path}"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            req = {}
        if self.path == "/v1/chat/completions":
            self._send({
                "id": "devstub-0", "object": "chat.completion",
                "model": req.get("model", MODEL),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant",
                                "content": f"dev-stub-reply:{MODEL}"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2},
            })
        else:
            self._send({"error": f"unknown path {self.path}"}, 404)

    def log_message(self, fmt, *args):  # quiet; systemd captures stderr
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
