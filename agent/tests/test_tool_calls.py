"""Tests for tool-call parsing and fake tool-roll detection."""

import json
import unittest

from app.tool_calls import is_fake_tool_roll_call, ollama_tool_calls


class FakeToolRollCallTests(unittest.TestCase):
    def test_detects_parenthetical_list(self) -> None:
        self.assertTrue(
            is_fake_tool_roll_call("(called tools: search_files, read_file)")
        )

    def test_ignores_real_prose(self) -> None:
        self.assertFalse(is_fake_tool_roll_call("I updated the search handler."))


class OllamaToolCallParseTests(unittest.TestCase):
    def test_native_function_block(self) -> None:
        msg = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "function": {
                        "name": "git_status",
                        "arguments": "{}",
                    }
                }
            ],
        }
        calls = ollama_tool_calls(msg)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "git_status")

    def test_flat_name_and_dict_arguments(self) -> None:
        msg = {
            "role": "assistant",
            "tool_calls": [
                {
                    "name": "search_files",
                    "arguments": {"pattern": "Stephen", "glob": "*.dart"},
                }
            ],
        }
        calls = ollama_tool_calls(msg)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "search_files")
        self.assertEqual(calls[0]["arguments"]["pattern"], "Stephen")

    def test_json_tool_call_in_content(self) -> None:
        msg = {
            "role": "assistant",
            "content": json.dumps(
                {"name": "list_directory", "arguments": {"path": "."}}
            ),
        }
        calls = ollama_tool_calls(msg)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "list_directory")


if __name__ == "__main__":
    unittest.main()