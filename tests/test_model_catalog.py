"""Model catalog, protocol-key resolution and upstream discovery tests."""

import asyncio
import importlib
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx


TEST_HOME = tempfile.mkdtemp(prefix="qodergate-catalog-")
ORIGINAL_HOME = os.environ.get("HOME")
os.environ["HOME"] = TEST_HOME
sys.path.insert(0, os.path.abspath("src"))

from fastapi.testclient import TestClient


catalog = importlib.import_module("qoder2api.catalog")
bridge = importlib.import_module("qoder2api.bridge")
models_module = importlib.import_module("qoder2api.models")
app_module = importlib.import_module("qoder2api.app")

if ORIGINAL_HOME is None:
    os.environ.pop("HOME", None)
else:
    os.environ["HOME"] = ORIGINAL_HOME


# The CN catalog must stay in sync with the Qoder CN client model cache.  Each
# pair is (catalog id, upstream protocol key) and mirrors configs[<id>].key.
LIVE_CN_MODELS = {
    "auto": "auto",
    "qwen3.8-max": "qmodel_38max",
    "qwen3.8-flash": "qfmodel",
    "qwen3.7-max": "qmodel_latest",
    "qwen3.7-plus": "qmodel",
    "qwen3.7-flash": "q37fmodel",
    "deepseek-v4-pro": "dmodel",
    "deepseek-flash": "dfmodel",
    "glm-5.3": "gmodel",
    "glm-5.3-flash": "gfmodel",
    "glm-5.2": "gm51model",
    "kimi-k3": "kmodel_latest",
    "kimi-k2.8-preview": "kmodel",
    "minimax-m2.7": "mmodel",
}


class CatalogSyncTests(unittest.TestCase):
    def test_cn_catalog_covers_every_live_model_with_its_key(self):
        entries = {entry["id"]: entry for entry in catalog.catalog_entries("cn")}
        for model_id, key in LIVE_CN_MODELS.items():
            self.assertIn(model_id, entries, f"CN catalog is missing {model_id}")
            self.assertEqual(entries[model_id]["key"], key, f"wrong upstream key for {model_id}")
        # `lite` is a deliberate compatibility alias for Auto, not an upstream id.
        self.assertEqual(entries["lite"]["key"], "auto")
        # Qwen3.6-Flash was delisted upstream and must not be advertised again.
        self.assertNotIn("qwen3.6-flash", entries)

    def test_every_advertised_id_resolves_to_its_catalog_key(self):
        for region in ("cn", "global"):
            for entry in catalog.catalog_entries(region):
                self.assertEqual(
                    catalog.model_key(region, entry["id"]),
                    entry["key"],
                    f"{region}/{entry['id']} is listed but would be forwarded with the wrong key",
                )

    def test_legacy_aliases_and_passthrough(self):
        self.assertEqual(catalog.cn_model_key("lite"), "auto")
        self.assertEqual(catalog.cn_model_key("qwen3.7plus"), "qmodel")
        self.assertEqual(catalog.cn_model_key("deepseek-v4-flash"), "dfmodel")
        self.assertEqual(catalog.cn_model_key("kimi-k2.7-code"), "kmodel")
        self.assertEqual(catalog.cn_model_key("Qwen3.8-MAX"), "qmodel_38max")
        # Unknown ids must pass through untouched rather than be dropped.
        self.assertEqual(catalog.cn_model_key("my-private-model"), "my-private-model")

    def test_global_keys_follow_the_client_static_catalog(self):
        self.assertEqual(catalog.global_model_key("lite"), "lite")
        self.assertEqual(catalog.global_model_key("Qwen3.7-Max"), "qmodel_latest")
        self.assertEqual(catalog.global_model_key("qwen3.8-max"), "qmodel_preview")


class ModelListParsingTests(unittest.TestCase):
    def test_parse_model_list_normalizes_and_filters(self):
        payload = {
            "chat": [
                {"key": "qmodel_38max", "display_name": "Qwen3.8-Max", "enable": True},
                {"key": "gm51model", "display_name": "GLM 5.2", "enable": True},
                {"key": "hidden", "display_name": "Hidden", "enable": False},
                {"key": "", "display_name": "No Key", "enable": True},
                {"key": "dup", "display_name": "Qwen3.8-Max", "enable": True},
            ]
        }
        rows = catalog.parse_model_list(payload)
        self.assertEqual([row["id"] for row in rows], ["qwen3.8-max", "glm-5.2"])
        self.assertEqual(rows[0]["key"], "qmodel_38max")
        self.assertEqual(rows[1]["label"], "GLM 5.2")

    def test_parse_model_list_rejects_unexpected_shapes(self):
        self.assertEqual(catalog.parse_model_list(None), [])
        self.assertEqual(catalog.parse_model_list({"chat": "nope"}), [])
        self.assertEqual(catalog.parse_model_list({"data": []}), [])


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.session = SimpleNamespace(
            region="cn",
            identity=SimpleNamespace(uid="uid-1", security_oauth_token="tok"),
        )

    def _run(self, sess=None):
        # provider_mode is patched so ambient QODER_PROVIDER_MODE from other test
        # modules cannot redirect discovery to the provider-config catalog.
        with patch.object(models_module, "provider_mode", return_value="standalone"):
            return asyncio.run(models_module.discover_models(self.session if sess is None else sess))

    def test_upstream_model_list_is_used_when_available(self):
        response = httpx.Response(
            200,
            json={"chat": [{"key": "gmodel", "display_name": "GLM-5.3", "enable": True}]},
            request=httpx.Request("GET", "https://gateway.qoder.com.cn/algo/api/v2/model/list?Encode=1"),
        )
        with patch.object(models_module.httpx, "AsyncClient", _client_returning(response)), \
             patch.object(models_module, "bearer_headers", return_value={}):
            discovered, source = self._run()
        self.assertEqual(source, "upstream")
        self.assertEqual([entry["id"] for entry in discovered], ["glm-5.3"])
        self.assertEqual(discovered[0]["key"], "gmodel")

    def test_static_catalog_is_the_fallback_when_discovery_fails(self):
        with patch.object(models_module.httpx, "AsyncClient", _client_raising(httpx.ConnectError("boom"))), \
             patch.object(models_module, "bearer_headers", return_value={}):
            discovered, source = self._run()
        self.assertEqual(source, "gateway-catalog")
        self.assertIn("qwen3.8-max", [entry["id"] for entry in discovered])

    def test_static_catalog_is_the_fallback_for_empty_upstream_list(self):
        response = httpx.Response(
            200,
            json={"chat": []},
            request=httpx.Request("GET", "https://gateway.qoder.com.cn/algo/api/v2/model/list?Encode=1"),
        )
        with patch.object(models_module.httpx, "AsyncClient", _client_returning(response)), \
             patch.object(models_module, "bearer_headers", return_value={}):
            discovered, source = self._run()
        self.assertEqual(source, "gateway-catalog")
        self.assertTrue(discovered)


class BridgeKeyTests(unittest.TestCase):
    def _body(self, region, model):
        request = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
        body, name, _ = bridge.build_qoder_body(request, SimpleNamespace(region=region))
        return body, name

    def test_cn_body_sends_the_catalog_key_and_keeps_the_requested_id(self):
        for model, key in LIVE_CN_MODELS.items():
            body, name = self._body("cn", model)
            self.assertEqual(body["model_config"]["key"], key, f"cn/{model} sent the wrong protocol key")
            self.assertEqual(body["chat_context"]["extra"]["modelConfig"]["key"], key)
            self.assertEqual(name, model)

    def test_global_body_sends_the_catalog_key_and_keeps_the_requested_id(self):
        body, name = self._body("global", "qwen3.7-max")
        self.assertEqual(body["model"], "qmodel_latest")
        self.assertEqual(name, "qwen3.7-max")
        # Known-good identity mapping must not change.
        body, _ = self._body("global", "lite")
        self.assertEqual(body["model"], "lite")


class ProviderListingTests(unittest.TestCase):
    def test_v1_models_lists_the_refreshed_catalog(self):
        client = TestClient(app_module.app, raise_server_exceptions=False)
        with patch.object(app_module, "load_config", return_value={
            "provider_mode": "standalone",
            "auth_required": False,
            "allowed_keys": [],
            "provider_api_key": None,
        }), \
             patch.object(app_module, "get_active_session", side_effect=RuntimeError("no account")), \
             patch.object(app_module, "configured_region", return_value="cn"), \
             patch.object(models_module, "provider_mode", return_value="standalone"):
            response = client.get("/v1/models")
        self.assertEqual(response.status_code, 200)
        ids = [item["id"] for item in response.json()["data"]]
        for model_id in LIVE_CN_MODELS:
            self.assertIn(model_id, ids)


def _client_returning(response):
    client = AsyncMock()
    client.__aenter__.return_value.get = AsyncMock(return_value=response)
    return lambda *args, **kwargs: client


def _client_raising(exc):
    client = AsyncMock()
    client.__aenter__.return_value.get = AsyncMock(side_effect=exc)
    return lambda *args, **kwargs: client


if __name__ == "__main__":
    unittest.main()
