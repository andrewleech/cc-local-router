"""Unit tests for the ~/.claude.json readers.

Every accessor is best-effort by contract, so the absent/malformed
cases are as load-bearing as the happy paths -- a broken config must
degrade the launcher, not stop it.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cc_local_router import claude_json


class ClaudeJsonTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        patcher = mock.patch.object(Path, "home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def write(self, data):
        (self.home / claude_json.CONFIG_NAME).write_text(json.dumps(data))


class ChannelArgsTests(ClaudeJsonTestCase):
    def test_global_servers_are_returned_prefixed(self):
        self.write({"mcpServers": {"claude-net": {}, "word": {}}})
        self.assertEqual(
            claude_json.channel_args("/somewhere"),
            ["server:claude-net", "server:word"],
        )

    def test_only_the_matching_project_scope_is_included(self):
        self.write({
            "mcpServers": {"global-one": {}},
            "projects": {
                "/proj/a": {"mcpServers": {"a-only": {}}},
                "/proj/b": {"mcpServers": {"b-only": {}}},
            },
        })
        self.assertEqual(
            claude_json.channel_args("/proj/a"),
            ["server:global-one", "server:a-only"],
        )

    def test_a_server_in_both_scopes_is_not_duplicated(self):
        self.write({
            "mcpServers": {"shared": {}},
            "projects": {"/proj/a": {"mcpServers": {"shared": {}}}},
        })
        self.assertEqual(claude_json.channel_args("/proj/a"), ["server:shared"])

    def test_missing_config_yields_no_args(self):
        self.assertEqual(claude_json.channel_args("/proj/a"), [])

    def test_malformed_config_yields_no_args(self):
        (self.home / claude_json.CONFIG_NAME).write_text("{not json")
        self.assertEqual(claude_json.channel_args("/proj/a"), [])

    def test_unexpected_shapes_are_tolerated(self):
        self.write({"mcpServers": ["not", "a", "dict"], "projects": 7})
        self.assertEqual(claude_json.channel_args("/proj/a"), [])


class HubUrlTests(ClaudeJsonTestCase):
    def test_hub_read_from_global_scope(self):
        self.write({"mcpServers": {"claude-net": {
            "env": {"CLAUDE_NET_HUB": "https://hub:4815"},
        }}})
        self.assertEqual(claude_json.hub_url(), "https://hub:4815")

    def test_hub_read_from_a_project_scope(self):
        self.write({"projects": {"/p": {"mcpServers": {"claude-net": {
            "env": {"CLAUDE_NET_HUB": "https://proj-hub:4815"},
        }}}}})
        self.assertEqual(claude_json.hub_url(), "https://proj-hub:4815")

    def test_no_claude_net_server_yields_none(self):
        self.write({"mcpServers": {"word": {"env": {"X": "1"}}}})
        self.assertIsNone(claude_json.hub_url())

    def test_claude_net_without_hub_env_yields_none(self):
        self.write({"mcpServers": {"claude-net": {"env": {}}}})
        self.assertIsNone(claude_json.hub_url())


class ApproveApiKeyTests(ClaudeJsonTestCase):
    KEY = "sk-ant-0123456789abcdefghijklmnop"

    def read(self):
        return json.loads((self.home / claude_json.CONFIG_NAME).read_text())

    def test_key_suffix_is_added_to_approved(self):
        self.write({"mcpServers": {}})
        claude_json.approve_api_key(self.KEY)
        self.assertEqual(
            self.read()["customApiKeyResponses"]["approved"], [self.KEY[-20:]],
        )

    def test_key_is_moved_out_of_rejected(self):
        self.write({"customApiKeyResponses": {
            "approved": [], "rejected": [self.KEY[-20:]],
        }})
        claude_json.approve_api_key(self.KEY)
        responses = self.read()["customApiKeyResponses"]
        self.assertEqual(responses["approved"], [self.KEY[-20:]])
        self.assertEqual(responses["rejected"], [])

    def test_rerunning_does_not_duplicate(self):
        self.write({"mcpServers": {}})
        claude_json.approve_api_key(self.KEY)
        claude_json.approve_api_key(self.KEY)
        self.assertEqual(
            self.read()["customApiKeyResponses"]["approved"], [self.KEY[-20:]],
        )

    def test_unrelated_config_is_preserved(self):
        self.write({"mcpServers": {"word": {"command": "x"}}, "theme": "dark"})
        claude_json.approve_api_key(self.KEY)
        data = self.read()
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["mcpServers"]["word"]["command"], "x")

    def test_missing_config_is_left_alone(self):
        claude_json.approve_api_key(self.KEY)
        self.assertFalse((self.home / claude_json.CONFIG_NAME).exists())

    def test_empty_key_is_a_no_op(self):
        self.write({"theme": "dark"})
        claude_json.approve_api_key("")
        self.assertEqual(self.read(), {"theme": "dark"})

    def test_no_temp_file_is_left_behind(self):
        self.write({"mcpServers": {}})
        claude_json.approve_api_key(self.KEY)
        leftovers = list(self.home.glob(".claude.json.*"))
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
