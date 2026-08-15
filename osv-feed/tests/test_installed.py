"""Tests for installed package version resolution."""

import unittest
from unittest import mock

from osv_feed.installed import _parse_os_release
from osv_feed.installed import host_ecosystem
from osv_feed.installed import installed_package
from osv_feed.installed import installed_version


class HostEcosystemTest(unittest.TestCase):
    def test_ubuntu_release(self):
        self.assertEqual(
            "Ubuntu",
            _parse_os_release('PRETTY_NAME="Ubuntu 24.04 LTS"\nID=ubuntu\n'),
        )

    def test_debian_release(self):
        self.assertEqual(
            "Debian",
            _parse_os_release('PRETTY_NAME="Debian GNU/Linux 13"\nID=debian\n'),
        )

    def test_other_release_has_no_default(self):
        self.assertIsNone(_parse_os_release('PRETTY_NAME="Fedora"\nID=fedora\n'))

    def test_missing_id_has_no_default(self):
        self.assertIsNone(_parse_os_release('PRETTY_NAME="Some OS"\n'))

    @mock.patch("osv_feed.installed.open", mock.mock_open(read_data='ID=debian\n'))
    def test_host_ecosystem_reads_os_release(self):
        self.assertEqual("Debian", host_ecosystem())


class InstalledVersionTest(unittest.TestCase):
    @staticmethod
    def _run(result):
        return mock.patch(
            "osv_feed.installed.subprocess.run",
            return_value=mock.Mock(returncode=0, stdout=result, stderr=""),
        )

    def test_binary_maps_to_source_package(self):
        output = (
            "openssh-client openssh 1:9.6p1-3ubuntu13.3\n"
            "openssh-server openssh 1:9.6p1-3ubuntu13.3\n"
        )
        with self._run(output):
            package = installed_package("openssh-client")
        self.assertEqual("openssh", package.name)
        self.assertEqual("1:9.6p1-3ubuntu13.3", package.version)

    def test_exact_binary_package_version(self):
        output = (
            "openssh-client openssh 1:9.6p1-3ubuntu13.3\n"
            "openssh-server openssh 1:9.6p1-3ubuntu13.3\n"
        )
        with self._run(output):
            self.assertEqual(
                "1:9.6p1-3ubuntu13.3", installed_version("openssh-server")
            )

    def test_source_package_fallback(self):
        output = "openssh-server openssh 1:9.6p1-3ubuntu13.3\n"
        with self._run(output):
            package = installed_package("openssh")
        self.assertEqual("openssh", package.name)
        self.assertEqual("1:9.6p1-3ubuntu13.3", package.version)

    def test_exact_binary_preferred_over_source(self):
        output = (
            "python3 python3 3.11.0-1\n"
            "python3-minimal python3 3.11.0-1\n"
            "python3-shared shared 3.12.0-1\n"
        )
        with self._run(output):
            package = installed_package("python3")
            self.assertEqual("python3", package.name)
            self.assertEqual("3.11.0-1", installed_version("python3"))

    def test_not_installed_raises(self):
        output = "openssh-client openssh 1:9.6p1-3ubuntu13.3\n"
        with self._run(output):
            with self.assertRaisesRegex(LookupError, "not installed"):
                installed_package("tmux")

    def test_dpkg_missing_raises(self):
        with mock.patch(
            "osv_feed.installed.subprocess.run",
            side_effect=OSError("no such file"),
        ):
            with self.assertRaisesRegex(LookupError, "cannot resolve"):
                installed_package("openssh")

    def test_dpkg_failure_raises(self):
        proc = mock.Mock(
            returncode=1,
            stdout="",
            stderr="dpkg-query: no packages found matching pattern openssh\n",
        )
        with mock.patch(
            "osv_feed.installed.subprocess.run", return_value=proc
        ):
            with self.assertRaisesRegex(LookupError, "cannot read installed"):
                installed_package("openssh")


if __name__ == "__main__":
    unittest.main()
