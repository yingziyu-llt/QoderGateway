import importlib
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx


TEST_HOME = tempfile.mkdtemp(prefix="qodergate-provider-")
ORIGINAL_HOME = os.environ.get("HOME")
os.environ["HOME"] = TEST_HOME
os.environ["QODER_PROVIDER_MODE"] = "new_api"
os.environ["QODER_PROVIDER_API_KEY"] = "channel-secret"
os.environ["QODER_PROVIDER_MODELS"] = "lite,alpha"
sys.path.insert(0, os.path.abspath("src"))

from fastapi.testclient import TestClient


app_module = importlib.import_module("qoder2api.app")
if ORIGINAL_HOME is None:
    os.environ.pop("HOME", None)
else:
    os.environ["HOME"] = ORIGINAL_HOME


class NewApiProviderContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app_module.app, raise_server_exceptions=False)

    def auth(self):
        return {"Authorization": "Bearer channel-secret"}

    def test_health_and_readiness(self):
        health = self.client.get("/healthz")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "ok"})

        ready = self.client.get("/readyz")
        self.assertEqual(ready.status_code, 503)
        self.assertEqual(ready.json()["status"], "not_ready")
        self.assertFalse(ready.json()["checks"]["enabled_accounts"])

    def test_provider_auth_and_stable_models(self):
        missing = self.client.get("/v1/models")
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(missing.json()["error"]["type"], "authentication_error")

        response = self.client.get("/v1/models", headers=self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.json()["data"]], ["lite", "alpha"])
        self.assertTrue(all(item["object"] == "model" for item in response.json()["data"]))

    def test_invalid_chat_request_and_unsupported_responses(self):
        invalid = self.client.post("/v1/chat/completions", headers=self.auth(), json={"model": "lite"})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json()["error"]["type"], "invalid_request_error")

        unsupported = self.client.post("/v1/responses", headers=self.auth(), json={})
        self.assertEqual(unsupported.status_code, 404)
        self.assertEqual(unsupported.json()["error"]["type"], "invalid_request_error")

    def test_non_stream_response_and_request_telemetry(self):
        fake_session = SimpleNamespace(
            identity=SimpleNamespace(uid="account-1", name="Test Account"),
            region="global",
        )
        captured = {}

        async def fake_completion(payload, session):
            captured.update(payload)
            return {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": payload["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
                "_qodergate_usage_estimated": True,
            }

        with patch.object(app_module, "db_load_accounts", return_value={"accounts": [{"uid": "account-1", "enabled": 1}]}), \
             patch.object(app_module, "get_session", new=AsyncMock(return_value=fake_session)), \
             patch.object(app_module, "complete_openai_response", new=fake_completion):
            response = self.client.post(
                "/v1/chat/completions",
                headers={**self.auth(), "X-Request-ID": "contract-123"},
                json={
                    "model": "alpha",
                    "messages": [{"role": "user", "content": "hello"}],
                    "tools": [{"type": "function", "function": {"name": "lookup"}}],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Request-ID"], "contract-123")
        self.assertEqual(response.json()["usage"]["total_tokens"], 3)
        self.assertNotIn("_qodergate_usage_estimated", response.json())
        self.assertEqual(captured["model"], "alpha")

        metrics = app_module.get_model_metrics()
        event = next(item for item in metrics["recent"] if item["request_id"] == "contract-123")
        self.assertEqual(event["source"], "new_api")
        self.assertTrue(event["tokens_estimated"])

    def test_stream_response_contract(self):
        fake_session = SimpleNamespace(
            identity=SimpleNamespace(uid="account-1", name="Test Account"),
            region="global",
        )

        async def fake_stream(payload, session, usage_state):
            usage_state.update({"usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}, "estimated": True})
            yield 'data: {"id":"chatcmpl-test","object":"chat.completion.chunk","created":1,"model":"lite","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
            yield 'data: {"id":"chatcmpl-test","object":"chat.completion.chunk","created":1,"model":"lite","choices":[{"index":0,"delta":{"content":"OK"},"finish_reason":null}]}\n\n'
            yield 'data: {"id":"chatcmpl-test","object":"chat.completion.chunk","created":1,"model":"lite","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}\n\n'
            yield "data: [DONE]\n\n"

        with patch.object(app_module, "db_load_accounts", return_value={"accounts": [{"uid": "account-1", "enabled": 1}]}), \
             patch.object(app_module, "get_session", new=AsyncMock(return_value=fake_session)), \
             patch.object(app_module, "stream_openai_response", new=fake_stream):
            response = self.client.post(
                "/v1/chat/completions",
                headers=self.auth(),
                json={"model": "lite", "messages": [{"role": "user", "content": "hello"}], "stream": True},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "text/event-stream; charset=utf-8")
        self.assertIn("[DONE]", response.text)
        self.assertIn('"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}', response.text)

    def test_quota_error_rotates_to_next_account(self):
        first = SimpleNamespace(identity=SimpleNamespace(uid="account-1", name="First"), region="global")
        second = SimpleNamespace(identity=SimpleNamespace(uid="account-2", name="Second"), region="global")
        success = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": "lite",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

        with patch.object(app_module, "db_load_accounts", return_value={"accounts": [{"uid": "account-1", "enabled": 1}, {"uid": "account-2", "enabled": 1}]}), \
             patch.object(app_module, "get_session", new=AsyncMock(side_effect=[first, second])), \
             patch.object(app_module, "complete_openai_response", new=AsyncMock(side_effect=[RuntimeError("HTTP 429 quota exceeded"), success])), \
             patch.object(app_module, "get_account_quota", return_value={"ok": True, "quota": {"isQuotaExceeded": True, "userQuota": {"remaining": 0}}}), \
             patch.object(app_module, "rotate_next_account") as rotate:
            response = self.client.post(
                "/v1/chat/completions",
                headers=self.auth(),
                json={"model": "lite", "messages": [{"role": "user", "content": "hello"}]},
            )

        self.assertEqual(response.status_code, 200)
        rotate.assert_called_once()

    def test_exhausted_quota_returns_rate_limit_error(self):
        session = SimpleNamespace(identity=SimpleNamespace(uid="account-1", name="Only"), region="global")
        with patch.object(app_module, "db_load_accounts", return_value={"accounts": [{"uid": "account-1", "enabled": 1}]}), \
             patch.object(app_module, "get_session", new=AsyncMock(return_value=session)), \
             patch.object(app_module, "complete_openai_response", new=AsyncMock(side_effect=RuntimeError("HTTP 429 quota exceeded"))), \
             patch.object(app_module, "get_account_quota", return_value={"ok": True, "quota": {"isQuotaExceeded": True, "userQuota": {"remaining": 0}}}), \
             patch.object(app_module, "rotate_next_account", side_effect=ValueError("no other account")):
            response = self.client.post(
                "/v1/chat/completions",
                headers=self.auth(),
                json={"model": "lite", "messages": [{"role": "user", "content": "hello"}]},
            )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["type"], "rate_limit_error")

    def test_upstream_timeout_returns_504_without_rotation(self):
        session = SimpleNamespace(identity=SimpleNamespace(uid="account-1", name="Only"), region="global")
        with patch.object(app_module, "db_load_accounts", return_value={"accounts": [{"uid": "account-1", "enabled": 1}]}), \
             patch.object(app_module, "get_session", new=AsyncMock(return_value=session)), \
             patch.object(app_module, "complete_openai_response", new=AsyncMock(side_effect=httpx.ReadTimeout("upstream timeout"))), \
             patch.object(app_module, "rotate_next_account") as rotate:
            response = self.client.post(
                "/v1/chat/completions",
                headers=self.auth(),
                json={"model": "lite", "messages": [{"role": "user", "content": "hello"}]},
            )

        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["error"]["type"], "upstream_timeout")
        rotate.assert_not_called()

    def test_stream_cache_usage_is_relayed_and_recorded(self):
        fake_session = SimpleNamespace(
            identity=SimpleNamespace(uid="account-1", name="Test Account"),
            region="global",
        )

        async def fake_stream(payload, session, usage_state):
            usage = {
                "prompt_tokens": 120,
                "completion_tokens": 8,
                "total_tokens": 128,
                "prompt_tokens_details": {"cached_tokens": 96},
            }
            usage_state.update({"usage": usage, "estimated": False})
            yield 'data: {"id":"chatcmpl-cache","object":"chat.completion.chunk","created":1,"model":"lite","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
            yield 'data: {"id":"chatcmpl-cache","object":"chat.completion.chunk","created":1,"model":"lite","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":120,"completion_tokens":8,"total_tokens":128,"prompt_tokens_details":{"cached_tokens":96}}}\n\n'
            yield "data: [DONE]\n\n"

        with patch.object(app_module, "db_load_accounts", return_value={"accounts": [{"uid": "account-1", "enabled": 1}]}), \
             patch.object(app_module, "get_session", new=AsyncMock(return_value=fake_session)), \
             patch.object(app_module, "stream_openai_response", new=fake_stream):
            response = self.client.post(
                "/v1/chat/completions",
                headers={**self.auth(), "X-Request-ID": "cache-123"},
                json={"model": "lite", "messages": [{"role": "user", "content": "hello"}], "stream": True},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn('"cached_tokens":96', response.text)
        metrics = app_module.get_model_metrics()
        event = next(item for item in metrics["recent"] if item["request_id"] == "cache-123")
        self.assertEqual(event["cached_tokens"], 96)
        self.assertFalse(event["tokens_estimated"])
        self.assertEqual(metrics["by_model"]["lite"]["cache_hit_rate"], 0.8)

        usage = self.client.get("/ui/api-keys/usage?window_hours=24", headers={"X-Gateway-Token": app_module.load_config().get("gateway_token", "admin")})
        self.assertEqual(usage.status_code, 200)
        self.assertEqual(usage.json()["cached_tokens"], 96)


if __name__ == "__main__":
    unittest.main()
