import unittest

from cluster_exec.truncate import truncate


class TruncateTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(truncate([], 5, 5), "")

    def test_shorter_than_window(self):
        lines = ["a", "b", "c"]
        self.assertEqual(truncate(lines, 5, 5), "a\nb\nc")

    def test_exactly_window(self):
        lines = ["a", "b", "c", "d", "e"]
        self.assertEqual(truncate(lines, 3, 2), "a\nb\nc\nd\ne")

    def test_needs_elision(self):
        lines = [str(i) for i in range(100)]
        out = truncate(lines, 10, 10)
        self.assertTrue(out.startswith("0\n1\n2\n3\n4\n5\n6\n7\n8\n9"))
        self.assertIn("… [80 lines elided] …", out)
        self.assertTrue(out.endswith("90\n91\n92\n93\n94\n95\n96\n97\n98\n99"))

    def test_zero_tail(self):
        lines = [str(i) for i in range(50)]
        out = truncate(lines, 5, 0)
        self.assertTrue(out.startswith("0\n1\n2\n3\n4"))
        self.assertIn("… [45 lines elided] …", out)
        self.assertFalse(out.endswith("49"))

    def test_negative_raises(self):
        with self.assertRaises(ValueError):
            truncate(["a"], -1, 5)
        with self.assertRaises(ValueError):
            truncate(["a"], 5, -1)

    def test_long_lines_preserved(self):
        big = "x" * 10_000
        out = truncate([big] * 5, 2, 2)
        self.assertIn(big, out)


if __name__ == "__main__":
    unittest.main()
