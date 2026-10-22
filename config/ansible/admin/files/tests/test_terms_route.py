"""Tests for the public root vhost's unlinked legal pages."""

import unittest
from pathlib import Path


def location_block(config, prefix):
    lines = config.splitlines()
    start = next(
        i for i, line in enumerate(lines)
        if line.startswith(prefix)
    )
    block = []
    for line in lines[start:]:
        block.append(line)
        if line == '}':
            break
    return '\n'.join(block)


class TermsRouteTests(unittest.TestCase):
    def setUp(self):
        self.config = (Path(__file__).parents[1] /
                       'ycluster_app-locations.conf.j2').read_text()

    def assert_public_static_and_unindexed(self, block):
        self.assertIn('root /var/www/html;', block)
        self.assertNotIn('admin-api-authentik-proxy.conf', block)
        self.assertIn('X-Robots-Tag "noindex, nofollow"', block)

    def test_generic_terms_is_public_static_and_unindexed(self):
        block = location_block(self.config, 'location = /terms {')
        self.assertIn('try_files /terms.html =404;', block)
        self.assert_public_static_and_unindexed(block)

    def test_deployment_terms_is_public_static_and_configurable(self):
        block = location_block(
            self.config,
            "location = /{{ ycluster_terms_path | default('legal/terms') }} {",
        )
        self.assertIn(
            "try_files /{{ ycluster_terms_file | default('terms.html') }} =404;",
            block,
        )
        self.assert_public_static_and_unindexed(block)


if __name__ == '__main__':
    unittest.main()
