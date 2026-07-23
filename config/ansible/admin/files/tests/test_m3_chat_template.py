"""Unit tests for the patched MiniMax-M3 chat template.

The stock M3 chat template raises ``raise_exception`` when a ``tool`` role
message appears without a preceding assistant tool call. Codex CLI produces
exactly this shape (the harness injects tool results into history), so every
hit becomes a 400 the proxy doesn't retry. The patched template drops the
guard entirely so orphaned tool messages render like any other turn.

These tests load the deployed template file directly with Jinja2 and assert
on the rendered text. No vLLM, no etcd, no Flask.

Run from config/ansible/admin/files:
    PYTHONPATH=. python3 -m unittest discover -s tests
or via ./run-tests.sh at the repo root.
"""

import os
import unittest

from jinja2 import Environment, FileSystemLoader

_HERE = os.path.dirname(os.path.abspath(__file__))      # .../admin/files/tests
_FILES = os.path.dirname(_HERE)                          # .../admin/files

env = Environment(loader=FileSystemLoader(_FILES), keep_trailing_newline=True)
TEMPLATE = env.get_template("minimax-m3-chat-template.jinja")


def render(messages, **kw):
    """Render the patched template with sane defaults."""
    return TEMPLATE.render(
        messages=messages,
        add_generation_prompt=kw.pop("add_generation_prompt", False),
        tools=kw.pop("tools", []),
        thinking_mode=kw.pop("thinking_mode", "adaptive"),
        **kw,
    )


class TestOrphanedToolMessage(unittest.TestCase):
    """The bug: a tool message with no preceding assistant tool call must
    not raise — it should render as a normal tool turn."""

    def test_orphaned_tool_message_renders(self):
        messages = [
            {"role": "user", "content": "Hi"},
            {"role": "tool", "content": "orphaned tool result"},
            {"role": "user", "content": "What is 2+2?"},
        ]
        out = render(messages)
        # The tool message rendered into the prompt, not rejected.
        self.assertIn("orphaned tool result", out)
        # The follow-up user turn is present.
        self.assertIn("What is 2+2?", out)

    def test_orphaned_tool_message_first_position(self):
        """A tool message as the very first conversation message (worst case
        for loop bounds) must still render without raising."""
        messages = [
            {"role": "tool", "content": "first turn is a tool result"},
        ]
        out = render(messages)
        self.assertIn("first turn is a tool result", out)

    def test_raise_exception_removed(self):
        """The patched template must not contain raise_exception — the guard
        that rejected orphaned tool messages is gone."""
        with open(os.path.join(_FILES, "minimax-m3-chat-template.jinja")) as f:
            src = f.read()
        self.assertNotIn("raise_exception", src)


class TestNormalToolFlow(unittest.TestCase):
    """A regular assistant tool_call -> tool result -> assistant final answer
    flow must still render correctly."""

    def test_tool_call_flow(self):
        messages = [
            {"role": "user", "content": "What is the weather?"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "get_weather",
                                          "arguments": {"location": "NYC"}}}]},
            {"role": "tool", "content": "Sunny, 72F"},
            {"role": "assistant", "content": "It is sunny and 72F in NYC."},
        ]
        out = render(messages, add_generation_prompt=True)
        # The assistant's tool call rendered.
        self.assertIn("get_weather", out)
        self.assertIn("NYC", out)
        # The tool result rendered.
        self.assertIn("Sunny, 72F", out)
        # The final assistant answer rendered.
        self.assertIn("It is sunny and 72F in NYC.", out)

    def test_multiple_tool_calls_in_one_assistant_turn(self):
        messages = [
            {"role": "user", "content": "Check weather and time"},
            {"role": "assistant", "content": "",
             "tool_calls": [
                 {"function": {"name": "get_weather",
                               "arguments": {"location": "NYC"}}},
                 {"function": {"name": "get_time",
                               "arguments": {"timezone": "EST"}}},
             ]},
            {"role": "tool", "content": "Sunny, 72F"},
            {"role": "tool", "content": "14:30 EST"},
            {"role": "assistant", "content": "Weather: Sunny 72F. Time: 14:30 EST."},
        ]
        out = render(messages, add_generation_prompt=True)
        self.assertIn("get_weather", out)
        self.assertIn("get_time", out)
        self.assertIn("Sunny, 72F", out)
        self.assertIn("14:30 EST", out)


class TestConversationsWithoutTools(unittest.TestCase):
    """Plain conversations (no tool calls at all) must render unchanged."""

    def test_plain_conversation(self):
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
            {"role": "user", "content": "What is 2+2?"},
            {"role": "assistant", "content": "4"},
        ]
        out = render(messages, add_generation_prompt=True)
        self.assertIn("Hello", out)
        self.assertIn("Hi there!", out)
        self.assertIn("What is 2+2?", out)
        self.assertIn("4", out)

    def test_generation_prompt_emits_bos_ai(self):
        """When add_generation_prompt=True, the prompt must end with the
        assistant-generation header so vLLM continues from there."""
        messages = [{"role": "user", "content": "Hi"}]
        out = render(messages, add_generation_prompt=True)
        # The template uses bos_token = ']~b]' and then 'ai' for assistant.
        self.assertTrue(out.rstrip().endswith("ai") or "]~b]ai" in out,
                        f"expected assistant generation header in: {out!r}")


class TestByteIdentityWithOriginal(unittest.TestCase):
    """For all non-orphaned-tool cases, the patched template must produce
    byte-identical output to the original M3 template. This catches
    accidental changes to the thinking-tag or token rendering."""

    @classmethod
    def setUpClass(cls):
        # The original M3 template is checked in at the MXFP4 checkpoint
        # directory on the cluster. For the unit test we look for a local
        # copy under tmp/orig-m3-template.jinja (created by the developer
        # during the patch work). If it's absent, we skip — the integration
        # test on the cluster is the source of truth for byte identity.
        cls.orig_path = os.path.join(
            os.path.dirname(_FILES), "..", "..", "tmp", "orig-m3-template.jinja"
        )
        cls.orig_path = os.path.abspath(cls.orig_path)
        if not os.path.exists(cls.orig_path):
            raise unittest.SkipTest(
                f"original M3 template not found at {cls.orig_path} — "
                "run the patch workflow first to generate it"
            )
        cls.orig_env = Environment(
            loader=FileSystemLoader(os.path.dirname(cls.orig_path)),
            keep_trailing_newline=True,
        )
        cls.orig_template = cls.orig_env.get_template(
            os.path.basename(cls.orig_path)
        )

    def _render_both(self, messages, **kw):
        a = self.orig_template.render(
            messages=messages,
            add_generation_prompt=kw.pop("add_generation_prompt", False),
            tools=kw.pop("tools", []),
            thinking_mode=kw.pop("thinking_mode", "adaptive"),
            **kw,
        )
        b = TEMPLATE.render(
            messages=messages,
            add_generation_prompt=kw.pop("add_generation_prompt", False),
            tools=kw.pop("tools", []),
            thinking_mode=kw.pop("thinking_mode", "adaptive"),
            **kw,
        )
        return a, b

    def test_plain_conversation_identical(self):
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        a, b = self._render_both(messages, add_generation_prompt=True)
        self.assertEqual(a, b)

    def test_tool_call_flow_identical(self):
        messages = [
            {"role": "user", "content": "What is the weather?"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "get_weather",
                                          "arguments": {"location": "NYC"}}}]},
            {"role": "tool", "content": "Sunny, 72F"},
            {"role": "assistant", "content": "It is sunny and 72F in NYC."},
        ]
        a, b = self._render_both(messages, add_generation_prompt=True)
        self.assertEqual(a, b)

    def test_multi_tool_identical(self):
        messages = [
            {"role": "user", "content": "Check weather and time"},
            {"role": "assistant", "content": "",
             "tool_calls": [
                 {"function": {"name": "get_weather",
                               "arguments": {"location": "NYC"}}},
                 {"function": {"name": "get_time",
                               "arguments": {"timezone": "EST"}}},
             ]},
            {"role": "tool", "content": "Sunny, 72F"},
            {"role": "tool", "content": "14:30 EST"},
            {"role": "assistant", "content": "Weather: Sunny 72F. Time: 14:30 EST."},
        ]
        a, b = self._render_both(messages, add_generation_prompt=True)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
