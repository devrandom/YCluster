"""Tests for HTTP service discovery of vLLM metrics backends.

The endpoint reads the inference registry from etcd and probes each
backend's /v1/models for its runtime. Both the etcd client and HTTP
probes are faked, so the suite is server-free.
"""

import os
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_FILES = os.path.dirname(_HERE)
for p in (_HERE, _FILES, os.path.join(_FILES, "ycluster")):
    if p not in sys.path:
        sys.path.insert(0, p)

from fake_etcd import FakeEtcd  # noqa: E402
import app as appmod  # noqa: E402


def _models(owner):
    response = mock.Mock(status_code=200)
    response.json.return_value = {"data": [{"id": "m", "owned_by": owner}]}
    return response


class InferenceTargetTests(unittest.TestCase):
    def setUp(self):
        self.etcd = FakeEtcd()
        self._orig_client = appmod.get_etcd_client
        appmod.get_etcd_client = lambda: self.etcd
        appmod._vllm_runtime_cache.clear()
        self.http = appmod.app.test_client()

    def tearDown(self):
        appmod.get_etcd_client = self._orig_client
        appmod._vllm_runtime_cache.clear()

    def backend(self, model, url):
        key = appmod.INFERENCE_MODELS_PREFIX + model
        record = self.etcd.get_json(key) or {"backends": []}
        record["backends"].append({"api_base": url})
        self.etcd.seed_json(key, record)

    def test_returns_only_vllm_backends(self):
        self.backend("ds4", "http://gpu1.xc:9216")
        self.backend("ds41", "http://gpu2.xc:9218")
        self.backend("glm", "http://gpu3.xc:9218")
        self.backend("ds4-alias", "http://gpu1.xc:9216")

        responses = {
            "http://gpu1.xc:9216/v1/models": _models("vllm"),
            "http://gpu2.xc:9218/v1/models": _models("vllm"),
            "http://gpu3.xc:9218/v1/models": _models("sglang"),
        }

        with mock.patch.object(appmod.requests, "get",
                               side_effect=lambda url, timeout=None: responses[url]):
            resp = self.http.get("/api/prometheus/targets/vllm")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), [
            {"targets": ["gpu1.xc:9216"],
             "labels": {"backend": "http://gpu1.xc:9216"}},
            {"targets": ["gpu2.xc:9218"],
             "labels": {"backend": "http://gpu2.xc:9218"}},
        ])

    def test_sglang_endpoint_returns_only_sglang_backends(self):
        self.backend("ds4", "http://gpu1.xc:9216")
        self.backend("glm", "http://gpu3.xc:9218")

        responses = {
            "http://gpu1.xc:9216/v1/models": _models("vllm"),
            "http://gpu3.xc:9218/v1/models": _models("sglang"),
        }

        with mock.patch.object(appmod.requests, "get",
                               side_effect=lambda url, timeout=None: responses[url]):
            resp = self.http.get("/api/prometheus/targets/sglang")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), [
            {"targets": ["gpu3.xc:9218"],
             "labels": {"backend": "http://gpu3.xc:9218"}},
        ])

    def test_unreachable_backend_is_excluded_and_probe_is_cached(self):
        self.backend("ds4", "http://gpu1.xc:9216")
        calls = []

        def fail(url, timeout=None):
            calls.append(url)
            raise appmod.requests.RequestException("unreachable")

        with mock.patch.object(appmod.requests, "get", side_effect=fail):
            first = self.http.get("/api/prometheus/targets/vllm")
            second = self.http.get("/api/prometheus/targets/vllm")

        self.assertEqual(first.get_json(), [])
        self.assertEqual(second.get_json(), [])
        self.assertEqual(len(calls), 1)

    def test_non_200_response_is_excluded(self):
        self.backend("ds4", "http://gpu1.xc:9216")
        response = mock.Mock(status_code=503)

        with mock.patch.object(appmod.requests, "get", return_value=response):
            resp = self.http.get("/api/prometheus/targets/vllm")

        self.assertEqual(resp.get_json(), [])

    def test_malformed_registry_entry_is_skipped(self):
        self.etcd.store[appmod.INFERENCE_MODELS_PREFIX + "broken"] = "not json"
        self.backend("ds4", "http://gpu1.xc:9216")

        with mock.patch.object(appmod.requests, "get",
                               return_value=_models("vllm")):
            resp = self.http.get("/api/prometheus/targets/vllm")

        self.assertEqual(resp.get_json(), [
            {"targets": ["gpu1.xc:9216"],
             "labels": {"backend": "http://gpu1.xc:9216"}},
        ])

    def test_etcd_failure_returns_503(self):
        def broken_client():
            raise RuntimeError("etcd unavailable")

        appmod.get_etcd_client = broken_client

        resp = self.http.get("/api/prometheus/targets/vllm")

        self.assertEqual(resp.status_code, 503)
        self.assertIn("etcd unavailable", resp.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
