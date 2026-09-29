"""CN (legacy encoded) request body conversion coverage.

The CN gateway expects the Qoder client-native message shape. Sending raw
OpenAI messages mostly works for plain text, but an assistant message that
carries tool calls with ``"content": null`` makes the upstream answer with an
empty completion (no reasoning, no content, ``finish_reason=stop``), which the
client observes as a sudden stop with no reply.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath("src"))

from qoder2api.bridge import build_cn_qoder_body  # noqa: E402

TOOL_CALLS = [
    {
        "id": "call-1",
        "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city":"北京"}'},
    }
]
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for a city",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        },
    }
]


def tool_conversation() -> dict:
    return {
        "model": "kimi-k3",
        "messages": [
            {"role": "user", "content": "北京现在天气怎么样？请调用工具查询。"},
            {"role": "assistant", "content": None, "tool_calls": TOOL_CALLS},
            {"role": "tool", "tool_call_id": "call-1", "name": "get_weather", "content": '{"city":"北京","temp_c":23}'},
        ],
        "tools": TOOLS,
    }


class CnBodyConversionTests(unittest.TestCase):
    def test_tool_conversation_is_converted_to_native_messages(self):
        body = build_cn_qoder_body(tool_conversation(), "kimi-k3")
        messages = body["messages"]
        self.assertEqual([message["role"] for message in messages], ["user", "assistant", "tool"])
        for message in messages:
            self.assertIsInstance(message.get("content"), str)
            self.assertIn("response_meta", message)
            self.assertIn("reasoning_content_signature", message)

        user, assistant, tool = messages
        self.assertEqual(user["content"], "")
        self.assertEqual(user["contents"], [{"type": "text", "text": "北京现在天气怎么样？请调用工具查询。"}])
        self.assertEqual(assistant["content"], "")
        self.assertEqual(assistant["tool_calls"][0]["id"], "call-1")
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "get_weather")
        self.assertEqual(tool["tool_call_id"], "call-1")
        self.assertEqual(tool["name"], "get_weather")
        self.assertEqual(body["tools"], TOOLS)
        self.assertEqual(body["chat_context"]["text"], "北京现在天气怎么样？请调用工具查询。")

    def test_assistant_text_messages_keep_their_text(self):
        request = {"model": "lite", "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]}
        body = build_cn_qoder_body(request, "lite")
        assistant = body["messages"][1]
        self.assertEqual(assistant["content"], "hello")

    def test_tool_calls_are_rendered_as_text_without_tools(self):
        request = {"model": "lite", "messages": [{"role": "assistant", "content": None, "tool_calls": TOOL_CALLS}]}
        body = build_cn_qoder_body(request, "lite")
        message = body["messages"][0]
        self.assertNotIn("tool_calls", message)
        self.assertTrue(message["content"].startswith("Tool calls:"))

    def test_tool_results_are_rendered_as_user_text_without_tools(self):
        request = {"model": "lite", "messages": [{"role": "tool", "tool_call_id": "call-1", "name": "get_weather", "content": '{"temp_c":23}'}]}
        body = build_cn_qoder_body(request, "lite")
        message = body["messages"][0]
        self.assertEqual(message["role"], "user")
        self.assertIn("Tool result (get_weather) [call-1]", message["contents"][0]["text"])

    def test_empty_and_non_dict_messages_are_skipped(self):
        request = {"model": "lite", "messages": [{"role": "assistant", "content": ""}, "broken", {"role": "user", "content": "hi"}]}
        body = build_cn_qoder_body(request, "lite")
        self.assertEqual([message["role"] for message in body["messages"]], ["user"])

    def test_content_lists_are_flattened_for_user_messages(self):
        request = {
            "model": "lite",
            "messages": [{"role": "user", "content": [{"type": "text", "text": "part one"}, {"type": "text", "text": "part two"}]}],
        }
        body = build_cn_qoder_body(request, "lite")
        self.assertEqual(body["messages"][0]["contents"][0]["text"], "part one\n\npart two")


if __name__ == "__main__":
    unittest.main()
