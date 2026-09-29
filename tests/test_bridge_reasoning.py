import asyncio
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


TEST_HOME = tempfile.mkdtemp(prefix="qodergate-bridge-")
os.environ["HOME"] = TEST_HOME
sys.path.insert(0, os.path.abspath("src"))

from qoder2api import bridge


def sse(body):
    return "data: " + json.dumps(body, separators=(",", ":")) + "\n\n"


class ReasoningForwardingTests(unittest.TestCase):
    def setUp(self):
        self.session = SimpleNamespace(region="cn")

    def test_extract_delta_reads_nested_reasoning_content(self):
        raw = sse(
            {
                "body": json.dumps(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "role": "assistant",
                                    "reasoning_content": "step",
                                }
                            }
                        ]
                    }
                )
            }
        )
        delta = bridge.extract_delta(raw[5:].strip())
        self.assertEqual(delta.role, "assistant")
        self.assertEqual(delta.reasoning_content, "step")
        self.assertFalse(delta.is_empty)

    def test_stream_forwards_reasoning_before_answer(self):
        upstream = [
            sse({"choices": [{"delta": {"role": "assistant", "reasoning_content": ""}}]}),
            sse({"choices": [{"delta": {"reasoning_content": "think "}}]}),
            sse({"choices": [{"delta": {"reasoning_content": "first"}}]}),
            sse({"choices": [{"delta": {"content": "answer"}}]}),
        ]

        async def fake_lines(sess, body, model):
            for line in upstream:
                yield line

        async def collect():
            state = {}
            chunks = []
            async for line in bridge.stream_openai_response(
                {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "hi"}], "stream": True},
                self.session,
                state,
            ):
                if line.startswith("data:") and line[5:].strip() != "[DONE]":
                    chunks.append(json.loads(line[5:])["choices"][0]["delta"])
            return chunks, state

        with patch.object(bridge, "qoder_stream_lines", new=fake_lines):
            chunks, state = asyncio.run(collect())

        self.assertEqual("".join(item.get("reasoning_content", "") for item in chunks), "think first")
        self.assertEqual("".join(item.get("content", "") for item in chunks), "answer")
        self.assertGreater(state["usage"]["completion_tokens"], 0)

    def test_non_stream_includes_reasoning_content(self):
        upstream = [
            sse({"choices": [{"delta": {"reasoning_content": "think "}}]}),
            sse({"choices": [{"delta": {"reasoning_content": "first"}}]}),
            sse({"choices": [{"delta": {"content": "answer"}}]}),
        ]

        async def fake_lines(sess, body, model):
            for line in upstream:
                yield line

        async def collect():
            return await bridge.complete_openai_response(
                {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "hi"}]},
                self.session,
            )

        with patch.object(bridge, "qoder_stream_lines", new=fake_lines):
            result = asyncio.run(collect())

        message = result["choices"][0]["message"]
        self.assertEqual(message["reasoning_content"], "think first")
        self.assertEqual(message["content"], "answer")


if __name__ == "__main__":
    unittest.main()
