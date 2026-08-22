"""Unit tests for the claude-v2 / claude-channels-v2 console scripts.

`_exec_patched` is stubbed throughout -- these cover the env shaping and
argv composition that happens before the exec, which is where all the
launcher's behaviour lives.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cc_local_router import launcher

_MANAGED_VARS = (
    *launcher._ENV_DEFAULTS,
    "ANTHROPIC_API_KEY",
    "CLAUDE_V2_FORCE_API_KEY",
    "CLAUDE_NET_PROXY_UPSTREAM",
    "CLAUDE_NET_CHANNELS_PATCHED",
)


class LauncherTestCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for var in _MANAGED_VARS:
            os.environ.pop(var, None)

        self.execs: list[list[str]] = []
        exec_patch = mock.patch.object(
            launcher, "_exec_patched",
            side_effect=lambda argv: self.execs.append(argv) or 0,
        )
        exec_patch.start()
        self.addCleanup(exec_patch.stop)
        self._set_proxy(True)

    def _set_proxy(self, healthy: bool):
        p = mock.patch.object(
            launcher.proxy_control, "ensure_running", return_value=healthy,
        )
        p.start()
        self.addCleanup(p.stop)


class EnvDefaultTests(LauncherTestCase):
    def test_defaults_are_applied(self):
        launcher.claude_v2([])
        self.assertEqual(
            os.environ["ANTHROPIC_BASE_URL"],
            launcher.proxy_control.DEFAULT_BASE_URL,
        )
        self.assertEqual(os.environ["ANTHROPIC_CUSTOM_MODEL_OPTION"], "local")
        self.assertEqual(os.environ["ENABLE_TOOL_SEARCH"], "true")

    def test_existing_values_are_not_overridden(self):
        os.environ["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:9999"
        os.environ["ANTHROPIC_CUSTOM_MODEL_OPTION"] = "mine"
        launcher.claude_v2([])
        self.assertEqual(os.environ["ANTHROPIC_BASE_URL"], "http://127.0.0.1:9999")
        self.assertEqual(os.environ["ANTHROPIC_CUSTOM_MODEL_OPTION"], "mine")

    def test_argv_reaches_the_binary_unchanged(self):
        launcher.claude_v2(["--print", "hi"])
        self.assertEqual(self.execs, [["--print", "hi"]])

    def test_settings_json_beats_the_builtin_default(self):
        # Claude Code applies settings.json's env block itself, so
        # exporting our default unconditionally would override the
        # user's own picker label.
        with mock.patch.object(
            launcher.claude_json, "settings_env",
            side_effect=lambda k: (
                "qwen3.8-27b" if k == "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"
                else None
            ),
        ):
            launcher.claude_v2([])
        self.assertEqual(
            os.environ["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"], "qwen3.8-27b",
        )
        self.assertEqual(os.environ["ANTHROPIC_CUSTOM_MODEL_OPTION"], "local")

    def test_the_environment_beats_settings_json(self):
        os.environ["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"] = "from-shell"
        with mock.patch.object(
            launcher.claude_json, "settings_env", return_value="from-settings",
        ):
            launcher.claude_v2([])
        self.assertEqual(
            os.environ["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"], "from-shell",
        )


class ApiKeyTests(LauncherTestCase):
    def test_a_stray_api_key_is_dropped(self):
        # A leftover key silently switches billing off the OAuth
        # subscription, so it must not survive into the child.
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-stray"
        launcher.claude_v2([])
        self.assertNotIn("ANTHROPIC_API_KEY", os.environ)

    def test_forced_key_is_honoured(self):
        os.environ["CLAUDE_V2_FORCE_API_KEY"] = "sk-ant-forced"
        with mock.patch.object(launcher.claude_json, "approve_api_key"):
            launcher.claude_v2([])
        self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "sk-ant-forced")

    def test_forced_key_is_pre_approved(self):
        os.environ["CLAUDE_V2_FORCE_API_KEY"] = "sk-ant-forced"
        with mock.patch.object(
            launcher.claude_json, "approve_api_key",
        ) as approve:
            launcher.claude_v2([])
        approve.assert_called_once_with("sk-ant-forced")


class DeadProxyFallbackTests(LauncherTestCase):
    def setUp(self):
        super().setUp()
        self._set_proxy(False)

    def test_base_url_is_cleared_so_the_session_still_works(self):
        launcher.claude_v2([])
        self.assertNotIn("ANTHROPIC_BASE_URL", os.environ)

    def test_a_configured_upstream_is_used_instead(self):
        os.environ["CLAUDE_NET_PROXY_UPSTREAM"] = "https://gw.example"
        launcher.claude_v2([])
        self.assertEqual(os.environ["ANTHROPIC_BASE_URL"], "https://gw.example")

    def test_the_binary_is_still_launched(self):
        launcher.claude_v2(["--version"])
        self.assertEqual(self.execs, [["--version"]])


class ChannelsV2Tests(LauncherTestCase):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        home = Path(self._tmp.name)
        (home / ".claude.json").write_text(json.dumps(
            {"mcpServers": {"claude-net": {}, "word": {}}},
        ))
        p = mock.patch.object(Path, "home", return_value=home)
        p.start()
        self.addCleanup(p.stop)
        agent = mock.patch.object(launcher, "_start_mirror_agent")
        agent.start()
        self.addCleanup(agent.stop)

    def test_channel_args_are_prepended_per_server(self):
        launcher.claude_channels_v2(["--print", "hi"])
        self.assertEqual(self.execs, [[
            "--dangerously-load-development-channels",
            "server:claude-net", "server:word",
            "--print", "hi",
        ]])

    def test_an_explicit_channel_flag_suppresses_injection(self):
        argv = ["--dangerously-load-development-channels", "server:only-this"]
        launcher.claude_channels_v2(list(argv))
        self.assertEqual(self.execs, [argv])

    def test_the_channels_patched_marker_is_exported(self):
        launcher.claude_channels_v2([])
        self.assertEqual(os.environ["CLAUDE_NET_CHANNELS_PATCHED"], "1")

    def test_no_mcp_servers_means_no_channel_flag(self):
        (Path.home() / ".claude.json").write_text("{}")
        launcher.claude_channels_v2(["-c"])
        self.assertEqual(self.execs, [["-c"]])


if __name__ == "__main__":
    unittest.main()
