#!/usr/bin/env python3
"""NEVER_SEND guardrail test: behavioral, not source-text.

Imports main.py and inspects the tools actually registered on the FastMCP
server. Asserts the registered set is exactly the documented read-only set
and that no tool name contains "send". Breaks in CI if a send capability is
re-registered, instead of breaking in front of real contacts.

Requires the mcp-server deps: pip install "mcp[cli]>=1.6.0" httpx requests.
Stdlib unittest style, run from this directory: python3 test_never_send.py
"""
from __future__ import annotations

import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

READ_ONLY_TOOLS = {
    "search_contacts",
    "list_messages",
    "list_chats",
    "get_chat",
    "get_direct_chat_by_contact",
    "get_contact_chats",
    "get_last_interaction",
    "get_message_context",
    "search_messages_semantic",
    "download_media",
}


def _registered_tool_names() -> set[str]:
    spec = importlib.util.spec_from_file_location(
        "wa_mcp_main", os.path.join(HERE, "main.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return {t.name for t in mod.mcp._tool_manager.list_tools()}


class NeverSendTest(unittest.TestCase):
    def test_registered_tools_are_exactly_the_read_only_set(self):
        self.assertEqual(_registered_tool_names(), READ_ONLY_TOOLS)

    def test_no_tool_name_mentions_send(self):
        for name in _registered_tool_names():
            self.assertNotIn("send", name, f"send-capable tool registered: {name}")


if __name__ == "__main__":
    unittest.main()
