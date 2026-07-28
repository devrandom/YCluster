import unittest

from pydantic import ValidationError

from cluster_exec.server import SERVER


class SchemaLockDownTests(unittest.TestCase):
    """Verify that no tool silently accepts unknown arguments.

    Without these checks, a caller passing an unrelated kwarg (e.g. a typo
    like `yield_time_ms` for `auto_bg_after_s`) gets no error -- Pydantic's
    default `extra="ignore"` drops the key and the function runs as if
    nothing happened. We forbid extras on every tool so such mistakes fail
    loudly.
    """

    def test_every_tool_advertises_no_additional_properties(self) -> None:
        tools = SERVER._tool_manager.list_tools()
        self.assertGreater(len(tools), 0, "no tools registered")
        for tool in tools:
            with self.subTest(tool=tool.name):
                self.assertIs(
                    tool.parameters.get("additionalProperties"),
                    False,
                    f"tool {tool.name!r} schema does not forbid additionalProperties",
                )

    def test_every_tool_rejects_unknown_argument(self) -> None:
        tools = SERVER._tool_manager.list_tools()
        self.assertGreater(len(tools), 0, "no tools registered")
        for tool in tools:
            with self.subTest(tool=tool.name):
                required = tool.parameters.get("required") or []
                baseline: dict[str, object] = {name: "x" for name in required}
                if "command" in baseline:
                    baseline["command"] = "echo hi"
                bogus = {**baseline, "yield_time_ms": 5000}
                with self.assertRaises(
                    ValidationError,
                    msg=f"tool {tool.name!r} accepted unknown kwarg",
                ):
                    tool.fn_metadata.arg_model.model_validate(bogus)

    def test_run_tool_rejects_known_typo_case(self) -> None:
        """Reproduce the original bug report: `yield_time_ms` on `run`."""
        run_tool = SERVER._tool_manager.get_tool("run")
        with self.assertRaises(ValidationError) as cm:
            run_tool.fn_metadata.arg_model.model_validate(
                {"command": "ls", "hostname": "s1", "yield_time_ms": 5000}
            )
        self.assertEqual(cm.exception.errors()[0]["type"], "extra_forbidden")

    def test_run_tool_no_longer_accepts_auto_bg_after_s(self) -> None:
        """`auto_bg_after_s` was removed when the foreground cap was
        fixed at 10s. Rejecting it loudly prevents silent no-ops if a
        caller (or an older prompt) still passes it."""
        run_tool = SERVER._tool_manager.get_tool("run")
        with self.assertRaises(ValidationError) as cm:
            run_tool.fn_metadata.arg_model.model_validate(
                {"command": "ls", "hostname": "s1", "auto_bg_after_s": 0}
            )
        self.assertEqual(cm.exception.errors()[0]["type"], "extra_forbidden")

    def test_run_tool_still_accepts_valid_arguments(self) -> None:
        run_tool = SERVER._tool_manager.get_tool("run")
        parsed = run_tool.fn_metadata.arg_model.model_validate(
            {"command": "ls", "hostname": "s1"}
        )
        self.assertEqual(parsed.model_dump_one_level()["command"], "ls")
        self.assertEqual(parsed.model_dump_one_level()["hostname"], "s1")


if __name__ == "__main__":
    unittest.main()
