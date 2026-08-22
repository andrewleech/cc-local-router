"""Unit tests for proxy entry-point resolution and bind derivation.

Process lifecycle (start/stop) is exercised by hand against a real Bun
process rather than here; these cover the pure logic that decides where
the proxy lives and what it must bind to.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cc_local_router import proxy_control


class EntryPointTests(unittest.TestCase):
    def setUp(self):
        for var in (
            "CC_LOCAL_ROUTER_PROXY", "CC_LOCAL_ROUTER_REPO",
        ):
            patcher = mock.patch.dict(os.environ, {}, clear=False)
            patcher.start()
            self.addCleanup(patcher.stop)
            os.environ.pop(var, None)

    def test_defaults_to_the_packaged_copy(self):
        entry = proxy_control.entry_point()
        self.assertEqual(entry.name, "index.ts")
        self.assertEqual(entry.parent.name, "proxy")
        self.assertEqual(entry.parent.parent.name, "cc_local_router")
        self.assertTrue(
            entry.is_file(),
            "the proxy must ship alongside the package, not just in the repo",
        )

    def test_explicit_override_wins(self):
        os.environ["CC_LOCAL_ROUTER_PROXY"] = "/tmp/custom/index.ts"
        self.assertEqual(
            proxy_control.entry_point(), Path("/tmp/custom/index.ts"),
        )

    def test_repo_override_points_into_the_working_tree(self):
        os.environ["CC_LOCAL_ROUTER_REPO"] = "/src/cc-local-router"
        self.assertEqual(
            proxy_control.entry_point(),
            Path("/src/cc-local-router/cc_local_router/proxy/index.ts"),
        )

    def test_explicit_override_beats_repo_override(self):
        os.environ["CC_LOCAL_ROUTER_PROXY"] = "/tmp/custom/index.ts"
        os.environ["CC_LOCAL_ROUTER_REPO"] = "/src/cc-local-router"
        self.assertEqual(
            proxy_control.entry_point(), Path("/tmp/custom/index.ts"),
        )


class BindEnvTests(unittest.TestCase):
    def test_host_and_port_are_taken_from_the_url(self):
        self.assertEqual(
            proxy_control.bind_env("http://127.0.0.1:9999"),
            {"CLAUDE_NET_PROXY_HOST": "127.0.0.1",
             "CLAUDE_NET_PROXY_PORT": "9999"},
        )

    def test_a_url_without_a_port_pins_only_the_host(self):
        self.assertEqual(
            proxy_control.bind_env("http://localhost"),
            {"CLAUDE_NET_PROXY_HOST": "localhost"},
        )

    def test_bind_env_matches_the_default_base_url(self):
        # If these drift, the launcher polls one port while the proxy
        # listens on another and startup times out.
        self.assertEqual(
            proxy_control.bind_env(proxy_control.DEFAULT_BASE_URL),
            {"CLAUDE_NET_PROXY_HOST": "127.0.0.1",
             "CLAUDE_NET_PROXY_PORT": "8787"},
        )


class LoopbackTests(unittest.TestCase):
    def test_loopback_urls_are_recognised(self):
        for url in ("http://127.0.0.1:8787", "http://localhost:1234"):
            self.assertTrue(proxy_control.is_loopback(url), url)

    def test_remote_urls_are_not_auto_started(self):
        for url in (
            "https://api.anthropic.com",
            "http://10.0.0.5:8787",
            "https://127.0.0.1:8787",  # TLS: not something we'd serve
        ):
            self.assertFalse(proxy_control.is_loopback(url), url)


class ProbeTests(unittest.TestCase):
    def test_an_unbound_port_is_not_healthy(self):
        # Port 9 (discard) is privileged, so nothing in a test run can
        # bind it; releasing a port we just bound would be racy.
        self.assertFalse(
            proxy_control.is_healthy("http://127.0.0.1:9", timeout=0.5),
        )


class StatePathTests(unittest.TestCase):
    def test_state_paths_live_under_the_home_state_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(Path, "home", return_value=Path(tmp)):
                self.assertEqual(
                    proxy_control.pid_path(),
                    Path(tmp) / ".local" / "state" / "cc-local-router"
                    / "proxy.pid",
                )
                self.assertTrue(proxy_control.state_dir().is_dir())


if __name__ == "__main__":
    unittest.main()
