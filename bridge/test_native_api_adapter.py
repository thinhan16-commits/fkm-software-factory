#!/usr/bin/env python3
"""
Unit tests for Native API Adapter (native_api_adapter.py).

Verifies:
1. Auto-session ID stability across multi-turn conversations:
   - [sys, user1] -> [sys, user1, asst1, user2] -> [sys, user1, asst1, user2, asst2(tool_calls), tool1, tool2]
2. Seed turn .md packaging when creating a new session with prior history or large system/tools prompt.
3. Resumed turn delta extraction:
   - No repeated system prompt if unchanged.
   - No repeated history.
   - All trailing role="tool" messages preserved.
4. Tool schema injection and <tool_call> parsing into OpenAI tool_calls format + finish_reason="tool_calls".
5. SSE stream chunk generation for both regular text and tool_calls.
6. Auto-rotation when turn count or character budget exceeds limits.
"""

import os
import json
import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from typing import List, Dict, Any

import native_api_adapter
from native_api_adapter import (
    NativeSessionTracker,
    build_tools_system_instruction,
    parse_tool_calls_from_response,
    compile_native_turn,
    format_sse_response_chunks,
)


class TestNativeApiAdapter(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store_path = os.path.join(self.temp_dir.name, "test_sessions.json")
        self.tracker = NativeSessionTracker(store_path=self.store_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # 1. Auto-Session ID Stability across multi-turn conversations
    # -------------------------------------------------------------------------
    def test_auto_session_id_stability(self):
        """Auto-session ID must remain identical as new assistant, user, and tool messages arrive."""
        provider = "chatgpt"

        # Turn 1: [sys, user1]
        req1 = SimpleNamespace(
            messages=[
                {"role": "system", "content": "You are an expert coding assistant."},
                {"role": "user", "content": "List files in the current repository."},
            ],
            user=None,
        )
        sid1, rotated1 = self.tracker.resolve_session_id(provider, req1)
        self.assertFalse(rotated1)
        self.assertTrue(sid1.startswith("native-"))

        # Turn 2: [sys, user1, asst1, user2]
        req2 = SimpleNamespace(
            messages=[
                {"role": "system", "content": "You are an expert coding assistant."},
                {"role": "user", "content": "List files in the current repository."},
                {"role": "assistant", "content": "I will check the directory contents."},
                {"role": "user", "content": "Also check package.json."},
            ],
            user=None,
        )
        sid2, rotated2 = self.tracker.resolve_session_id(provider, req2)
        self.assertFalse(rotated2)
        self.assertEqual(sid1, sid2, "Session ID should be stable across conversation turns")

        # Turn 3: [sys, user1, asst1, user2, asst2(tool_calls), tool1, tool2]
        req3 = SimpleNamespace(
            messages=[
                {"role": "system", "content": "You are an expert coding assistant."},
                {"role": "user", "content": "List files in the current repository."},
                {"role": "assistant", "content": "I will check the directory contents."},
                {"role": "user", "content": "Also check package.json."},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path": "package.json"}'},
                        },
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {"name": "list_dir", "arguments": '{"path": "."}'},
                        },
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": '{"name": "test-pkg"}'},
                {"role": "tool", "tool_call_id": "call_2", "name": "list_dir", "content": '["package.json", "index.js"]'},
            ],
            user=None,
        )
        sid3, rotated3 = self.tracker.resolve_session_id(provider, req3)
        self.assertFalse(rotated3)
        self.assertEqual(sid1, sid3, "Session ID must remain stable after assistant tool calls and tool outputs")

    def test_auto_session_user_field(self):
        """When client provides req.user, auto-session ID should use native-user-{sanitized}."""
        provider = "chatgpt"
        req = SimpleNamespace(
            messages=[{"role": "user", "content": "Hello!"}],
            user="alice-workspace/project_1",
        )
        sid, rotated = self.tracker.resolve_session_id(provider, req)
        self.assertFalse(rotated)
        self.assertEqual(sid, "native-user-alice-workspace_project_1")

    # -------------------------------------------------------------------------
    # 2. Seed turn .md packaging
    # -------------------------------------------------------------------------
    def test_seed_turn_markdown_packaging_with_prior_history(self):
        """When starting a new session tab with existing history, package history into .md seed file."""
        req = SimpleNamespace(
            messages=[
                {"role": "system", "content": "You are an AI architect."},
                {"role": "user", "content": "Build me a fast web crawler."},
                {"role": "assistant", "content": "I designed the architecture using asyncio."},
                {"role": "user", "content": "Now add SQLite storage."},
            ],
            tools=None,
            tool_choice=None,
            response_format=None,
        )
        prompt_to_send, seed_file, is_json, sys_hash = compile_native_turn(
            req,
            "session-abc-123",
            is_resumed_session=False,
            temp_dir=self.temp_dir.name,
        )
        self.assertIsNotNone(seed_file)
        self.assertTrue(os.path.exists(seed_file))
        self.assertTrue(os.path.basename(seed_file).startswith("context_seed_session-abc-123"))

        # Verify seed file content
        with open(seed_file, "r", encoding="utf-8") as f:
            file_body = f.read()
        self.assertIn("You are an AI architect.", file_body)
        self.assertIn("Build me a fast web crawler.", file_body)
        self.assertIn("I designed the architecture using asyncio.", file_body)

        # Inline composer prompt should be concise and refer to the attached seed file
        self.assertIn("[CONTEXT ATTACHED]", prompt_to_send)
        self.assertIn(os.path.basename(seed_file), prompt_to_send)
        self.assertIn("Now add SQLite storage.", prompt_to_send)

    def test_seed_turn_markdown_packaging_with_oversized_system(self):
        """When system + tools exceeds SEED_FILE_THRESHOLD_CHARS, package into .md seed file."""
        large_system = "A" * 7000
        req = SimpleNamespace(
            messages=[
                {"role": "system", "content": large_system},
                {"role": "user", "content": "Hello world."},
            ],
            tools=None,
            tool_choice=None,
            response_format=None,
        )
        prompt_to_send, seed_file, is_json, sys_hash = compile_native_turn(
            req,
            "large-seed-test",
            is_resumed_session=False,
            temp_dir=self.temp_dir.name,
        )
        self.assertIsNotNone(seed_file)
        self.assertTrue(os.path.exists(seed_file))
        self.assertIn("[CONTEXT ATTACHED]", prompt_to_send)
        self.assertIn("Hello world.", prompt_to_send)

    # -------------------------------------------------------------------------
    # 3. Resumed turn delta extraction
    # -------------------------------------------------------------------------
    def test_resumed_turn_delta_extraction(self):
        """Resumed session must only send trailing delta messages and skip unchanged system & prior turns."""
        system_text = "System instruction 1."
        tools = [{"type": "function", "function": {"name": "run_bash", "parameters": {}}}]
        req = SimpleNamespace(
            messages=[
                {"role": "system", "content": system_text},
                {"role": "user", "content": "Initial user request"},
                {"role": "assistant", "content": "I am working on it."},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "call_99", "type": "function", "function": {"name": "run_bash", "arguments": '{"cmd": "ls"}'}},
                        {"id": "call_100", "type": "function", "function": {"name": "run_bash", "arguments": '{"cmd": "pwd"}'}},
                    ],
                },
                {"role": "tool", "tool_call_id": "call_99", "name": "run_bash", "content": "file1.py\nfile2.py"},
                {"role": "tool", "tool_call_id": "call_100", "name": "run_bash", "content": "/home/project"},
            ],
            tools=tools,
            tool_choice=None,
            response_format=None,
        )

        # 1. Compile seed turn to compute system_hash
        _, _, _, initial_sys_hash = compile_native_turn(
            req, "sess-delta", is_resumed_session=False, temp_dir=self.temp_dir.name
        )

        # 2. Resumed turn with identical system_hash
        prompt_to_send, seed_file, is_json, new_sys_hash = compile_native_turn(
            req, "sess-delta", is_resumed_session=True, known_system_hash=initial_sys_hash
        )

        self.assertIsNone(seed_file, "Resumed turn should not produce a seed file attachment")
        # System instructions should NOT be repeated
        self.assertNotIn("System instruction 1.", prompt_to_send)
        self.assertNotIn("Initial user request", prompt_to_send)
        self.assertNotIn("I am working on it.", prompt_to_send)

        # All trailing tool messages after the last assistant turn must be present!
        self.assertIn("Tool [run_bash] (call_id: call_99):", prompt_to_send)
        self.assertIn("file1.py\nfile2.py", prompt_to_send)
        self.assertIn("Tool [run_bash] (call_id: call_100):", prompt_to_send)
        self.assertIn("/home/project", prompt_to_send)

    def test_resumed_turn_system_hash_update(self):
        """If system prompt changed on a resumed turn, emit updated system instructions."""
        req = SimpleNamespace(
            messages=[
                {"role": "system", "content": "Updated system instructions for specialized role."},
                {"role": "assistant", "content": "Ready."},
                {"role": "user", "content": "Execute step 2."},
            ],
            tools=None,
            tool_choice=None,
            response_format=None,
        )
        prompt_to_send, seed_file, is_json, new_sys_hash = compile_native_turn(
            req, "sess-update", is_resumed_session=True, known_system_hash="old_stale_hash"
        )
        self.assertIn("### [UPDATED SYSTEM INSTRUCTIONS & TOOLS]:", prompt_to_send)
        self.assertIn("Updated system instructions for specialized role.", prompt_to_send)
        self.assertIn("Execute step 2.", prompt_to_send)

    # -------------------------------------------------------------------------
    # 4. Tool Schema Injection & <tool_call> Parsing
    # -------------------------------------------------------------------------
    def test_tool_schema_injection_and_tool_choice(self):
        """Verify OpenAI tools schema formatting and tool_choice behavior."""
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "search_code",
                    "description": "Searches for symbols in the codebase",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                },
            }
        ]

        instruction_auto = build_tools_system_instruction(tools, tool_choice="auto")
        self.assertIn("search_code", instruction_auto)
        self.assertIn("<tool_call>", instruction_auto)

        instruction_required = build_tools_system_instruction(tools, tool_choice="required")
        self.assertIn("[CRITICAL TOOL_CHOICE]: 'required'", instruction_required)

        instruction_specific = build_tools_system_instruction(
            tools, tool_choice={"type": "function", "function": {"name": "search_code"}}
        )
        self.assertIn("You MUST invoke the tool 'search_code'", instruction_specific)

    def test_parse_tool_calls_from_response(self):
        """Extracts <tool_call> blocks into OpenAI tool_calls structure and strips from content."""
        tools = [{"type": "function", "function": {"name": "read_file"}}]
        raw_response = (
            "I will examine the configuration file now.\n"
            "<tool_call>\n"
            '{"name": "read_file", "arguments": {"path": "/etc/config.json"}}\n'
            "</tool_call>"
        )

        content, tool_calls = parse_tool_calls_from_response(raw_response, tools)
        self.assertEqual(content, "I will examine the configuration file now.")
        self.assertIsNotNone(tool_calls)
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["function"]["name"], "read_file")
        self.assertEqual(
            json.loads(tool_calls[0]["function"]["arguments"]),
            {"path": "/etc/config.json"},
        )
        self.assertTrue(tool_calls[0]["id"].startswith("call_"))

    def test_parse_multiple_tool_calls_empty_content(self):
        """When response contains only tool calls, content should be None (OpenAI standard)."""
        tools = [
            {"type": "function", "function": {"name": "tool_a"}},
            {"type": "function", "function": {"name": "tool_b"}},
        ]
        raw_response = (
            "<tool_call>\n"
            '{"name": "tool_a", "arguments": {"key": 1}}\n'
            "</tool_call>\n"
            "<tool_call>\n"
            '{"name": "tool_b", "arguments": {"key": 2}}\n'
            "</tool_call>"
        )
        content, tool_calls = parse_tool_calls_from_response(raw_response, tools)
        self.assertIsNone(content, "Content must be None when response contains only tool calls")
        self.assertEqual(len(tool_calls), 2)
        self.assertEqual(tool_calls[0]["function"]["name"], "tool_a")
        self.assertEqual(tool_calls[1]["function"]["name"], "tool_b")

    def test_parse_tool_calls_fenced_fallback(self):
        """Fallback to fenced ```tool_call or ```json matching declared tool names."""
        tools = [{"type": "function", "function": {"name": "fetch_api"}}]
        raw_response = (
            "Calling API:\n"
            "```tool_call\n"
            '{"name": "fetch_api", "arguments": {"endpoint": "/v1/status"}}\n'
            "```"
        )
        content, tool_calls = parse_tool_calls_from_response(raw_response, tools)
        self.assertEqual(content, "Calling API:")
        self.assertIsNotNone(tool_calls)
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["function"]["name"], "fetch_api")

    # -------------------------------------------------------------------------
    # 5. SSE Stream Chunk Generation
    # -------------------------------------------------------------------------
    def test_sse_streaming_regular_content(self):
        """Tests SSE streaming generator for regular text output."""
        async def run_test():
            chunks = []
            async for sse in format_sse_response_chunks(
                "resp-123",
                "gpt-4o",
                content="Hello, how can I assist you today?",
                reasoning_content="User asked for assistance.",
            ):
                chunks.append(sse)

            # Check that chunks are SSE data: lines ending with \n\n
            self.assertTrue(all(c.startswith("data: ") and c.endswith("\n\n") for c in chunks))

            payloads = [json.loads(c[6:].strip()) for c in chunks if c[6:].strip() != "[DONE]"]
            self.assertEqual(chunks[-1], "data: [DONE]\n\n")

            # First chunk is role delta
            self.assertEqual(payloads[0]["choices"][0]["delta"]["role"], "assistant")
            # Next chunk is reasoning_content
            self.assertEqual(payloads[1]["choices"][0]["delta"]["reasoning_content"], "User asked for assistance.")
            # Next chunk is content
            self.assertEqual(payloads[2]["choices"][0]["delta"]["content"], "Hello, how can I assist you today?")
            # Final chunk has finish_reason: "stop"
            self.assertEqual(payloads[3]["choices"][0]["finish_reason"], "stop")

        asyncio.run(run_test())

    def test_sse_streaming_tool_calls(self):
        """Tests SSE streaming generator for tool calls."""
        async def run_test():
            tool_calls = [
                {
                    "id": "call_abc123",
                    "type": "function",
                    "function": {"name": "run_command", "arguments": '{"cmd": "pytest"}'},
                }
            ]
            chunks = []
            async for sse in format_sse_response_chunks(
                "resp-tool",
                "gpt-4o",
                content=None,
                tool_calls=tool_calls,
                stream_options={"include_usage": True},
                usage={"prompt_tokens": 50, "completion_tokens": 15, "total_tokens": 65},
            ):
                chunks.append(sse)

            payloads = [json.loads(c[6:].strip()) for c in chunks if c[6:].strip() != "[DONE]"]
            self.assertEqual(chunks[-1], "data: [DONE]\n\n")

            # Look for tool call delta chunk
            tc_payloads = [
                p for p in payloads
                if p.get("choices") and "tool_calls" in p["choices"][0].get("delta", {})
            ]
            self.assertTrue(len(tc_payloads) >= 1)
            self.assertEqual(tc_payloads[0]["choices"][0]["delta"]["tool_calls"][0]["id"], "call_abc123")

            # Look for finish_reason: "tool_calls" chunk
            finish_payloads = [p for p in payloads if p.get("choices") and p["choices"][0].get("finish_reason") == "tool_calls"]
            self.assertEqual(len(finish_payloads), 1)

            # Look for usage chunk
            usage_payloads = [p for p in payloads if "usage" in p]
            self.assertEqual(len(usage_payloads), 1)
            self.assertEqual(usage_payloads[0]["usage"]["total_tokens"], 65)

        asyncio.run(run_test())

    # -------------------------------------------------------------------------
    # 6. Auto-rotation when turn count or character budget exceeds limits
    # -------------------------------------------------------------------------
    def test_auto_rotation_on_turn_and_char_limits(self):
        """Auto-rotation increments gen counter and refreshes session tab ID."""
        os.environ["BRIDGE_MAX_SESSION_TURNS"] = "3"
        os.environ["BRIDGE_MAX_SESSION_CHARS"] = "500"

        tracker = NativeSessionTracker(store_path=os.path.join(self.temp_dir.name, "rotation.json"))
        req = SimpleNamespace(
            messages=[
                {"role": "user", "content": "Task initial seed."},
            ],
            user=None,
        )

        sid_g1, rotated_g1 = tracker.resolve_session_id("chatgpt", req)
        self.assertFalse(rotated_g1)
        self.assertNotIn("-g2", sid_g1)

        # Simulate 3 turns
        tracker.record_turn(sid_g1, prompt_chars=50, response_chars=50)
        tracker.record_turn(sid_g1, prompt_chars=50, response_chars=50)
        tracker.record_turn(sid_g1, prompt_chars=50, response_chars=50)

        # 4th turn: exceeds turn limit (3 turns)
        sid_g2, rotated_g2 = tracker.resolve_session_id("chatgpt", req)
        self.assertTrue(rotated_g2, "Session must rotate after exceeding MAX_SESSION_TURNS")
        self.assertEqual(sid_g2, f"{sid_g1}-g2")


if __name__ == "__main__":
    unittest.main()
