"""Prompt-cache (KV cache) usage coverage for the protocol bridge.

Covers the upstream usage shapes seen across Qoder's modern and legacy
gateways and asserts that cache counters survive the trip to the OpenAI
compatible response.
"""
import asyncio
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath("src"))

from qoder2api.bridge import (  # noqa: E402
    build_qoder_body,
    complete_openai_response,
    estimate_usage,
    extract_delta,
    normalize_usage,
    stream_openai_response,
)

UPSTREAM_USAGE = {
    "prompt_tokens": 120,
    "completion_tokens": 8,
    "total_tokens": 128,
    "prompt_tokens_details": {"cached_tokens": 96},
    "completion_tokens_details": {"reasoning_tokens": 2},
}


def sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, separators=(",", ":")) + "\n\n"


class NormalizeUsageTests(unittest.TestCase):
    def test_openai_details_are_preserved(self):
        usage = normalize_usage(
            {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110, "prompt_tokens_details": {"cached_tokens": 80}}
        )
        self.assertEqual(usage["prompt_tokens_details"], {"cached_tokens": 80})

    def test_simple_usage_and_detailed_raw_usage_are_merged(self):
        usage = normalize_usage(
            {
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                "raw_usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110, "prompt_tokens_details": {"cached_tokens": 80}},
            }
        )
        self.assertEqual(usage["prompt_tokens_details"], {"cached_tokens": 80})

    def test_deepseek_cache_counters_map_to_cached_tokens(self):
        usage = normalize_usage(
            {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110, "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20}
        )
        self.assertEqual(usage["prompt_tokens_details"]["cached_tokens"], 80)
        self.assertEqual(usage["prompt_cache_hit_tokens"], 80)
        self.assertEqual(usage["prompt_cache_miss_tokens"], 20)

    def test_anthropic_cache_read_widens_prompt_total(self):
        usage = normalize_usage(
            {"usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30, "cache_read_input_tokens": 80, "cache_creation_input_tokens": 5}}
        )
        self.assertEqual(usage["prompt_tokens"], 105)
        self.assertEqual(usage["total_tokens"], 115)
        self.assertEqual(usage["prompt_tokens_details"]["cached_tokens"], 80)

    def test_legacy_response_meta_usage_is_found(self):
        usage = normalize_usage(
            {"response_meta": {"usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110, "prompt_tokens_details": {"cached_tokens": 60}}}}
        )
        self.assertEqual(usage["prompt_tokens_details"]["cached_tokens"], 60)

    def test_zeroed_echo_does_not_shadow_nested_usage(self):
        usage = normalize_usage(
            {
                "prompt_tokens": 0,
                "total_tokens": 0,
                "response_meta": {"usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110, "prompt_tokens_details": {"cached_tokens": 70}}},
            }
        )
        self.assertEqual(usage["prompt_tokens"], 100)
        self.assertEqual(usage["prompt_tokens_details"]["cached_tokens"], 70)

    def test_reasoning_tokens_are_kept(self):
        usage = normalize_usage({"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3, "completion_tokens_details": {"reasoning_tokens": 7}})
        self.assertEqual(usage["completion_tokens_details"], {"reasoning_tokens": 7})

    def test_usage_free_payload_returns_none(self):
        self.assertIsNone(normalize_usage({"choices": [{"delta": {"content": "hi"}}]}))

    def test_extract_delta_reads_legacy_body_envelope(self):
        inner = {"response_meta": {"usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110, "prompt_tokens_details": {"cached_tokens": 55}}}}
        delta = extract_delta(json.dumps({"body": json.dumps(inner)}))
        self.assertEqual(delta.usage["prompt_tokens_details"]["cached_tokens"], 55)

    def test_estimated_usage_keeps_openai_shape(self):
        usage = estimate_usage({"messages": [{"role": "user", "content": "hello"}]}, "world")
        self.assertEqual(usage["prompt_tokens_details"], {"cached_tokens": 0})
        self.assertEqual(usage["total_tokens"], usage["prompt_tokens"] + usage["completion_tokens"])


class BridgePassthroughTests(unittest.TestCase):
    def session(self):
        return SimpleNamespace(region="global", identity=SimpleNamespace(security_oauth_token="dt-test"))

    def fake_lines(self):
        async def generator(sess, body, model):
            yield sse({"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
            yield sse({"choices": [{"index": 0, "delta": {"content": "OK"}, "finish_reason": None}]})
            yield sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": UPSTREAM_USAGE})
            yield "data: [DONE]\n\n"

        return generator

    def test_stream_response_keeps_cache_details(self):
        request = {"model": "lite", "messages": [{"role": "user", "content": "hi"}], "stream": True}
        usage_state: dict = {}
        with patch("qoder2api.bridge.qoder_stream_lines", new=self.fake_lines()):
            chunks = asyncio.run(self.collect(stream_openai_response(request, self.session(), usage_state)))
        usage_chunks = [json.loads(chunk[len("data: ") :]) for chunk in chunks if chunk.startswith("data: {")]
        final = usage_chunks[-1]
        self.assertEqual(final["usage"]["prompt_tokens_details"]["cached_tokens"], 96)
        self.assertEqual(final["usage"]["completion_tokens_details"]["reasoning_tokens"], 2)
        self.assertEqual(usage_state["usage"]["prompt_tokens_details"]["cached_tokens"], 96)
        self.assertFalse(usage_state["estimated"])

    def test_complete_response_keeps_cache_details(self):
        request = {"model": "lite", "messages": [{"role": "user", "content": "hi"}]}
        with patch("qoder2api.bridge.qoder_stream_lines", new=self.fake_lines()):
            response = asyncio.run(complete_openai_response(request, self.session()))
        self.assertEqual(response["usage"]["prompt_tokens_details"]["cached_tokens"], 96)
        self.assertFalse(response["_qodergate_usage_estimated"])

    def test_cache_routing_keys_are_forwarded(self):
        request = {"model": "lite", "messages": [{"role": "user", "content": "hi"}], "prompt_cache_key": "cache-abc", "user": "user-1"}
        body, _, _ = build_qoder_body(request, self.session())
        self.assertEqual(body["prompt_cache_key"], "cache-abc")
        self.assertEqual(body["user"], "user-1")

    async def collect(self, iterator):
        return [chunk async for chunk in iterator]


if __name__ == "__main__":
    unittest.main()
