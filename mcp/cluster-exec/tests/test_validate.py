import unittest

from cluster_exec.validate import validate_command


class ValidateCommandTests(unittest.TestCase):
    def allowed(self, command: str) -> None:
        self.assertIsNone(
            validate_command(command), f"expected allowed, got: {validate_command(command)!r}"
        )

    def denied(self, command: str, fragment: str) -> None:
        msg = validate_command(command)
        self.assertIsNotNone(msg, f"expected denied, got allowed: {command!r}")
        self.assertIn(fragment, msg or "")

    def test_ssh_denied(self) -> None:
        self.denied("ssh host cmd", "use the hostname argument")

    def test_ssh_denied_in_pipe(self) -> None:
        self.denied("echo hi | ssh host", "use the hostname argument")

    def test_head_denied_without_minus_one(self) -> None:
        self.denied("head file", "head_lines")

    def test_tail_denied_without_minus_one(self) -> None:
        self.denied("tail file", "tail_lines")

    def test_head_tail_allowed_with_minus_one(self) -> None:
        self.allowed("head -1 file")
        self.allowed("tail -1 file")
        self.allowed("head -1 file | tail -1")

    def test_headtail_always_denied(self) -> None:
        self.denied("headtail file", "headtail")
        self.denied("headtail -1 file", "headtail")

    def test_sleep_short_allowed(self) -> None:
        self.allowed("sleep 1")
        self.allowed("sleep 10")
        self.allowed("sleep 0.5s")

    def test_sleep_long_denied(self) -> None:
        self.denied("sleep 11", "sleep > 10s")
        self.denied("sleep 1m", "sleep > 10s")

    def test_sleep_unknown_denied(self) -> None:
        self.denied("sleep $NOPE", "sleep > 10s")

    def test_subshells_not_checked(self) -> None:
        self.allowed("bash -c 'head file'")
        self.allowed("bash -c \"tail file\"")
        self.allowed("x=$(tail -1 f)")
        self.allowed("echo $(head x)")
        self.allowed("(head x) && echo hi")

    def test_arguments_not_checked(self) -> None:
        self.allowed("echo head tail ssh")
        self.allowed("sudo sleep 30")

    def test_separators_start_new_command(self) -> None:
        self.denied("ls; ssh host", "use the hostname argument")
        self.denied("ls && head file", "head_lines")
        self.allowed("ls; head -1 file")

    def test_balanced_command_returns_none(self) -> None:
        self.allowed("ls -la")


if __name__ == "__main__":
    unittest.main()
