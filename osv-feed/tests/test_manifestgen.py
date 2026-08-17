import json
import os
import shutil
import tempfile
import unittest

from osv_feed import manifestgen

UV_FIXTURE = """\
version = 1
revision = 3
requires-python = ">=3.11"

[[package]]
name = "requests"
version = "2.32.3"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "urllib3" }]
sdist = { url = "https://example/requests.tar.gz", hash = "sha256:x", size = 1 }
wheels = []

[[package]]
name = "urllib3"
version = "2.2.1"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://example/urllib3.tar.gz", hash = "sha256:y", size = 1 }
wheels = []

[[package]]
name = "webapp-server"
version = "0.1.0"
source = { virtual = "." }
dependencies = [{ name = "urllib3" }]
dev-dependencies = { prod = [{ name = "requests" }, { name = "django", extra = ["argon2"] }] }
metadata = { requires-dist = [] }

[[package]]
name = "django"
version = "5.2.1"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://example/django.tar.gz", hash = "sha256:z", size = 1 }
wheels = []

[[package]]
name = "black"
version = "24.4.2"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://example/black.tar.gz", hash = "sha256:w", size = 1 }
wheels = []
"""


class SpecsFromUvLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="osv-mgen-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.uv = os.path.join(self.tmp, "uv.lock")
        with open(self.uv, "w", encoding="utf-8") as fh:
            fh.write(UV_FIXTURE)

    def test_returns_every_package_sorted_excluding_virtual_root(self):
        # The virtual root project (webapp-server) is not a PyPI package and is
        # excluded; every installable package is returned.
        self.assertEqual(
            [
                ("black", "24.4.2"),
                ("django", "5.2.1"),
                ("requests", "2.32.3"),
                ("urllib3", "2.2.1"),
            ],
            manifestgen.specs_from_uv_lock(self.uv),
        )

    def test_group_restricts_to_closure(self):
        # prod pulls requests + django (and their transitive deps); black is
        # dev-only and must be excluded.
        self.assertEqual(
            [
                ("django", "5.2.1"),
                ("requests", "2.32.3"),
                ("urllib3", "2.2.1"),
            ],
            manifestgen.specs_from_uv_lock(self.uv, select="prod"),
        )

    def test_unknown_group_returns_nothing(self):
        self.assertEqual([], manifestgen.specs_from_uv_lock(self.uv, select="nope"))

    def test_base_restricts_to_project_dependencies(self):
        # base = the root project's own dependencies (urllib3), not the
        # dev/prod dependency groups or dev-only black.
        self.assertEqual(
            [("urllib3", "2.2.1")],
            manifestgen.specs_from_uv_lock(self.uv, select="base"),
        )

    def test_deduplicates_duplicate_names(self):
        # A package can appear more than once across resolution markers.
        self.assertEqual(4, len(manifestgen.specs_from_uv_lock(self.uv)))


def _make_package_lock(packages):
    doc = {"name": "app", "version": "0.1.0", "lockfileVersion": 3, "packages": packages}
    return json.dumps(doc)


class SpecsFromPackageLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="osv-mgen-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.pl = os.path.join(self.tmp, "package-lock.json")

    def test_scoped_and_nested_names(self):
        packages = {
            "": {"name": "app", "version": "0.1.0"},
            "node_modules/@scope/pkg": {"version": "1.2.0"},
            "node_modules/foo/node_modules/bar": {"version": "2.0.0"},
            "node_modules/linkdep": {"version": "3.0.0", "link": True},
        }
        with open(self.pl, "w", encoding="utf-8") as fh:
            fh.write(_make_package_lock(packages))
        self.assertEqual(
            [("@scope/pkg", "1.2.0"), ("bar", "2.0.0"), ("linkdep", "3.0.0")],
            manifestgen.specs_from_package_lock(self.pl),
        )

    def test_skips_root_and_versionless_entries(self):
        packages = {
            "": {"name": "app", "version": "0.1.0"},
            "node_modules/a": {"version": "1.0.0"},
            "node_modules/workspace": {},
        }
        with open(self.pl, "w", encoding="utf-8") as fh:
            fh.write(_make_package_lock(packages))
        self.assertEqual([("a", "1.0.0")], manifestgen.specs_from_package_lock(self.pl))

    def test_legacy_dependencies_fallback(self):
        doc = {
            "name": "app",
            "version": "0.1.0",
            "lockfileVersion": 1,
            "dependencies": {"left-pad": {"version": "1.3.0"}},
        }
        with open(self.pl, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(doc))
        self.assertEqual(
            [("left-pad", "1.3.0")], manifestgen.specs_from_package_lock(self.pl)
        )


class GenerateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="osv-mgen-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.uv = os.path.join(self.tmp, "uv.lock")
        with open(self.uv, "w", encoding="utf-8") as fh:
            fh.write(UV_FIXTURE)
        self.pl = os.path.join(self.tmp, "package-lock.json")
        with open(self.pl, "w", encoding="utf-8") as fh:
            fh.write(
                _make_package_lock(
                    {"node_modules/@scope/pkg": {"version": "1.2.0"}}
                )
            )

    def test_renders_ecosystem_lines(self):
        lines = manifestgen.generate(
            "image", uv_lock=self.uv, package_lock=self.pl, comment="pin abc"
        )
        self.assertEqual(lines[0], "# pin abc")
        self.assertIn("PyPI:requests=2.32.3", lines)
        self.assertIn("npm:@scope/pkg=1.2.0", lines)

    def test_missing_input_contributes_nothing(self):
        lines = manifestgen.generate("image", uv_lock=os.path.join(self.tmp, "nope"))
        self.assertEqual([], lines)


if __name__ == "__main__":
    unittest.main()
