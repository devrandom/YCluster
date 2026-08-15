import unittest

from osv_feed import categories


class CategoryPackagesTest(unittest.TestCase):
    def test_packages(self):
        self.assertEqual(
            ["openssh-client", "nginx"],
            categories.category_packages({"packages": ["openssh-client", "nginx"]}),
        )

    def test_packages_missing_is_empty(self):
        self.assertEqual([], categories.category_packages({}))

    def test_packages_requires_strings(self):
        with self.assertRaises(ValueError):
            categories.category_packages({"packages": ["openssh", 3]})


class PackageBelongsTest(unittest.TestCase):
    def test_matches_source_name(self):
        self.assertTrue(categories.package_belongs("openssh", {"packages": ["openssh"]}))
        self.assertFalse(categories.package_belongs("nginx", {"packages": ["openssh"]}))

    def test_matches_name_with_version(self):
        self.assertTrue(
            categories.package_belongs("openssh", {"packages": ["openssh=1.0"]})
        )

    def test_case_insensitive(self):
        self.assertTrue(categories.package_belongs("OpenSSH", {"packages": ["openssh"]}))


class CategoryEcosystemTest(unittest.TestCase):
    def test_no_default_when_config_and_default_omitted(self):
        self.assertIsNone(categories.category_ecosystem({}))

    def test_falls_back_to_default_when_configured_ecosystem_missing(self):
        self.assertEqual("Debian", categories.category_ecosystem({}, default="Debian"))

    def test_configured(self):
        self.assertEqual("Debian", categories.category_ecosystem({"ecosystem": "Debian"}))


class PassesCategoryTest(unittest.TestCase):
    def test_severity_threshold(self):
        cfg = {"severity": "high"}
        self.assertTrue(categories.passes_category({"severity": "critical"}, cfg))
        self.assertTrue(categories.passes_category({"severity": "high"}, cfg))
        self.assertFalse(categories.passes_category({"severity": "medium"}, cfg))
        self.assertFalse(categories.passes_category({"severity": ""}, cfg))

    def test_states(self):
        cfg = {"states": ["no_fix_yet", "fix_available"]}
        self.assertTrue(categories.passes_category({"state": "no_fix_yet"}, cfg))
        self.assertFalse(categories.passes_category({"state": "patched"}, cfg))

    def test_interests_match_summary_or_details(self):
        cfg = {"interests": ["privilege escalation", "local root"]}
        self.assertTrue(
            categories.passes_category(
                {"summary": "", "details": "local privilege escalation via sudo"}, cfg
            )
        )
        self.assertTrue(
            categories.passes_category({"summary": "local root takeover", "details": ""}, cfg)
        )
        self.assertFalse(
            categories.passes_category({"summary": "tls leak", "details": "info leak"}, cfg)
        )

    def test_no_filters_passes_everything(self):
        self.assertTrue(categories.passes_category({"severity": "low"}, {}))

    def test_invalid_severity_raises(self):
        with self.assertRaises(ValueError):
            categories.passes_category({"severity": "blocker"}, {"severity": "blocker"})


class MatchedInterestsTest(unittest.TestCase):
    def test_returns_matching_interests(self):
        cfg = {"interests": ["rce", "remote code execution", "privilege escalation"]}
        self.assertEqual(
            ["rce", "remote code execution"],
            categories.matched_interests(
                {"summary": "RCE in sshd", "details": "remote code execution before release"}, cfg
            ),
        )

    def test_case_insensitive_and_filters_nonmatches(self):
        cfg = {"interests": ["rce", "csrf"]}
        self.assertEqual(
            ["rce"],
            categories.matched_interests({"summary": "RCE in sshd", "details": ""}, cfg),
        )

    def test_empty_without_interests(self):
        self.assertEqual([], categories.matched_interests({"summary": "x"}, {}))

    def test_empty_when_no_interest_matches(self):
        cfg = {"interests": ["privilege escalation"]}
        self.assertEqual(
            [], categories.matched_interests({"summary": "tls info leak", "details": ""}, cfg)
        )


if __name__ == "__main__":
    unittest.main()
