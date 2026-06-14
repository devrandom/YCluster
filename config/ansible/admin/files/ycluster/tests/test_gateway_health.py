"""Unit tests for the gateway-VIP health logic (ycluster.utils.gateway_health).

Pure functions and the healthy/unhealthy decision matrix only; the network and
etcd I/O (carrier read, TCP probe, next-hop ping, live etcd read) are
monkeypatched so nothing here touches a socket or etcd.

Run from the package root (config/ansible/admin/files/ycluster):
    python3 -m unittest tests.test_gateway_health
"""

import unittest

from ycluster.utils import gateway_health as gh


class ParseRatholePortTests(unittest.TestCase):
    def test_str_value(self):
        self.assertEqual(
            gh.parse_rathole_port('{"remote_addr": "host.ycluster.net:2333"}'), 2333)

    def test_bytes_value(self):
        self.assertEqual(
            gh.parse_rathole_port(b'{"remote_addr": "host:5000"}'), 5000)

    def test_ipv4_host(self):
        self.assertEqual(
            gh.parse_rathole_port('{"remote_addr": "203.0.113.7:2333"}'), 2333)

    def test_missing_value_uses_default(self):
        self.assertEqual(gh.parse_rathole_port(None), gh.DEFAULT_RATHOLE_PORT)
        self.assertEqual(gh.parse_rathole_port(b''), gh.DEFAULT_RATHOLE_PORT)

    def test_no_remote_addr_uses_default(self):
        self.assertEqual(gh.parse_rathole_port('{"token": "x"}'),
                         gh.DEFAULT_RATHOLE_PORT)

    def test_malformed_json_uses_default(self):
        self.assertEqual(gh.parse_rathole_port('not json'),
                         gh.DEFAULT_RATHOLE_PORT)

    def test_non_numeric_port_uses_default(self):
        self.assertEqual(gh.parse_rathole_port('{"remote_addr": "host:abc"}'),
                         gh.DEFAULT_RATHOLE_PORT)

    def test_no_port_uses_default(self):
        self.assertEqual(gh.parse_rathole_port('{"remote_addr": "host"}'),
                         gh.DEFAULT_RATHOLE_PORT)

    def test_explicit_default(self):
        self.assertEqual(gh.parse_rathole_port(None, default=9), 9)


class FrontendProbeIpsTests(unittest.TestCase):
    def test_extracts_ips(self):
        nodes = [{"name": "f1", "ip": "1.1.1.1"}, {"name": "f2", "ip": "2.2.2.2"}]
        self.assertEqual(gh.frontend_probe_ips(nodes), ["1.1.1.1", "2.2.2.2"])

    def test_skips_hostname_only(self):
        nodes = [{"name": "f1", "hostname": "f1.example"}, {"name": "f2", "ip": "2.2.2.2"}]
        self.assertEqual(gh.frontend_probe_ips(nodes), ["2.2.2.2"])

    def test_empty(self):
        self.assertEqual(gh.frontend_probe_ips([]), [])


class CheckGatewayTests(unittest.TestCase):
    """Drive check_gateway() with the probes/etcd-read stubbed out."""

    def setUp(self):
        # Sensible defaults; individual tests override what they exercise.
        self._patch('carrier_up', lambda iface: True)
        self._patch('read_targets', lambda timeout=gh.ETCD_TIMEOUT: (["1.1.1.1", "2.2.2.2"], 2333))
        self._patch('tcp_reachable', lambda iface, ip, port, timeout=gh.PROBE_TIMEOUT: False)
        self._patch('next_hop_reachable', lambda iface: False)

    def _patch(self, name, fn):
        orig = getattr(gh, name)
        setattr(gh, name, fn)
        self.addCleanup(setattr, gh, name, orig)

    def test_no_carrier_fails_fast(self):
        self._patch('carrier_up', lambda iface: False)
        # Even if everything else would pass, carrier-down short-circuits.
        self._patch('tcp_reachable', lambda *a, **k: True)
        ok, _ = gh.check_gateway("up0")
        self.assertFalse(ok)

    def test_first_reachable_frontend_is_healthy(self):
        self._patch('tcp_reachable',
                    lambda iface, ip, port, timeout=gh.PROBE_TIMEOUT: ip == "2.2.2.2")
        ok, msg = gh.check_gateway("up0")
        self.assertTrue(ok)
        self.assertIn("2.2.2.2", msg)

    def test_no_frontend_reachable_fails_even_if_nexthop_up(self):
        # The whole point: frontends unreachable => this uplink is a bad gateway,
        # so we must fail (let the VIP move) rather than fall back to next-hop.
        self._patch('next_hop_reachable', lambda iface: True)
        ok, _ = gh.check_gateway("up0")
        self.assertFalse(ok)

    def test_etcd_down_falls_back_to_nexthop_up(self):
        def boom(timeout=gh.ETCD_TIMEOUT):
            raise RuntimeError("etcd unreachable")
        self._patch('read_targets', boom)
        self._patch('next_hop_reachable', lambda iface: True)
        ok, msg = gh.check_gateway("up0")
        self.assertTrue(ok)
        self.assertIn("etcd unavailable", msg)

    def test_etcd_down_and_nexthop_down_fails(self):
        def boom(timeout=gh.ETCD_TIMEOUT):
            raise RuntimeError("etcd unreachable")
        self._patch('read_targets', boom)
        ok, _ = gh.check_gateway("up0")
        self.assertFalse(ok)

    def test_no_frontends_falls_back_to_nexthop(self):
        self._patch('read_targets', lambda timeout=gh.ETCD_TIMEOUT: ([], 2333))
        self._patch('next_hop_reachable', lambda iface: True)
        ok, msg = gh.check_gateway("up0")
        self.assertTrue(ok)
        self.assertIn("no frontends configured", msg)


class MainTests(unittest.TestCase):
    def test_usage_without_iface(self):
        self.assertEqual(gh.main([]), 2)

    def test_exit_codes_track_health(self):
        orig = gh.check_gateway
        try:
            gh.check_gateway = lambda iface: (True, "ok")
            self.assertEqual(gh.main(["up0"]), 0)
            gh.check_gateway = lambda iface: (False, "bad")
            self.assertEqual(gh.main(["up0"]), 1)
        finally:
            gh.check_gateway = orig


if __name__ == "__main__":
    unittest.main()
