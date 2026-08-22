"""Console-script launchers for the model-alias-patched Claude Code.

`claude-v2` sets the env the model picker and proxy need, starts the
proxy on demand, and execs the patched binary. `claude-channels-v2`
layers claude-net's runtime pieces -- per-MCP-server channel args and
the mirror-agent daemon -- on top.

Patching is delegated to `cc_patcher.launch.resolve_patched_binary()`,
imported directly rather than shelled out to: cc-patcher is a hard
dependency of this package, so it is always importable from the same
environment, which makes the launcher independent of PATH.
"""

import os
import subprocess
import sys
from pathlib import Path

from . import claude_json, proxy_control

# Env defaults applied by claude-v2. Each is only set if absent, so
# anything already exported by the user wins.
#
# ANTHROPIC_CUSTOM_MODEL_OPTION* put the alias in the /model picker with
# a proper label. The patched binary already accepts the alias at every
# validation gate; these only affect what the interactive picker renders.
#
# ANTHROPIC_BASE_URL points at the local model-splitter proxy, which
# routes the alias to a local inference server and everything else to
# api.anthropic.com.
_ENV_DEFAULTS: dict[str, str] = {
    "ANTHROPIC_CUSTOM_MODEL_OPTION": "local",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "Local",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION": (
        "Local inference server (via cc-local-router proxy)"
    ),
    "ANTHROPIC_BASE_URL": proxy_control.DEFAULT_BASE_URL,
    # Force Tool Search on. Without this, Claude Code's optimistic
    # heuristic silently disables tool-search deferral when
    # ANTHROPIC_BASE_URL points somewhere other than api.anthropic.com --
    # MCP schemas then get loaded upfront and eat 25-30k+ tokens of
    # context per session. Setting the env var makes the client send the
    # `tool-search-tool-2025-10-19` beta header regardless of provider
    # detection; the proxy is a pure passthrough so the header reaches
    # Anthropic and deferral kicks in.
    "ENABLE_TOOL_SEARCH": "true",
    # Raise the stream idle timeout. The client aborts a streaming
    # response if no bytes arrive within this window. Defaults vary
    # (180s first-party, 300s other) but a Statsig experiment can push
    # it down to ~60s, which fires spuriously during extended thinking
    # where Anthropic sometimes goes 60-90s between output chunks. Env
    # var beats the experiment; clamp is 1..1800000 ms.
    "CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS": "600000",
    # Disable the byte watchdog entirely as a belt-and-braces measure.
    # Even with the timeout above bumped, an ~58s abort still fires --
    # likely because the watchdog's abort path is driven by a companion
    # timer that hasn't been traced. Disabling lets the raw fetch stream
    # flow without client-side idle enforcement.
    "CLAUDE_ENABLE_BYTE_WATCHDOG": "false",
    "CLAUDE_ENABLE_STREAM_WATCHDOG": "false",
}

_CHANNEL_FLAGS = ("--dangerously-load-development-channels", "--channels")
_MIRROR_AGENT = Path.home() / ".local" / "bin" / "claude-net-mirror-agent"
_DEFAULT_HUB = "http://localhost:4815"


def _apply_env_defaults() -> None:
    """Environment first, then ~/.claude/settings.json, then the
    built-in default -- the same precedence the proxy uses.

    Consulting settings.json matters: Claude Code applies that file's
    `env` block itself, but an explicitly-exported variable beats it, so
    unconditionally exporting a default here would silently override the
    user's own picker label or backend URL.
    """
    for key, value in _ENV_DEFAULTS.items():
        if key in os.environ:
            continue
        os.environ[key] = claude_json.settings_env(key) or value


def _settle_api_key() -> None:
    """Keep a stray ANTHROPIC_API_KEY from hijacking OAuth login.

    When ANTHROPIC_API_KEY is set, Claude Code prefers it over the OAuth
    login in ~/.claude/.credentials.json -- even a leftover value in the
    outer shell silently switches billing away from the user's
    subscription. Drop it unless CLAUDE_V2_FORCE_API_KEY opts back in.
    """
    forced = os.environ.get("CLAUDE_V2_FORCE_API_KEY")
    if forced:
        os.environ["ANTHROPIC_API_KEY"] = forced
        claude_json.approve_api_key(forced)
    else:
        os.environ.pop("ANTHROPIC_API_KEY", None)


def _exec_patched(argv: list[str]) -> int:
    """Resolve + patch + cache the real binary, then exec it.

    argv[0] is the returned path, which is the stable `claude-patched`
    symlink rather than its hash-versioned target -- downstream
    argv[0]-anchored matching (claude-net's mirror agent) depends on the
    name ending in exactly that.
    """
    from cc_patcher.launch import BinaryNotFoundError, resolve_patched_binary

    try:
        patched = resolve_patched_binary()
    except BinaryNotFoundError as exc:
        print(f"[claude-v2] {exc}", file=sys.stderr)
        return 1
    os.execv(str(patched), [str(patched), *argv])


def _bypass_dead_proxy() -> None:
    """Point Claude Code straight at the upstream when the proxy is down.

    ANTHROPIC_BASE_URL otherwise still names the loopback port the proxy
    failed to bind, which fails every request rather than just losing
    alias routing. Losing the alias is recoverable; losing the session
    is not.
    """
    fallback = os.environ.get("CLAUDE_NET_PROXY_UPSTREAM")
    if fallback:
        os.environ["ANTHROPIC_BASE_URL"] = fallback
    else:
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        fallback = "Claude Code's default upstream"
    print(
        f"[claude-v2] proxy unavailable; sending traffic to {fallback}. "
        f"The '{os.environ['ANTHROPIC_CUSTOM_MODEL_OPTION']}' alias will "
        f"not route to a local backend this session.",
        file=sys.stderr,
    )


def claude_v2(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _apply_env_defaults()
    _settle_api_key()
    if not proxy_control.ensure_running():
        _bypass_dead_proxy()
    return _exec_patched(argv)


def _start_mirror_agent() -> None:
    """Start claude-net's mirror agent if installed and not already up.

    Silently does nothing when claude-net isn't installed -- the channel
    patches and the mirror agent are independent.
    """
    if not os.access(_MIRROR_AGENT, os.X_OK):
        return
    run_dir = Path("/tmp/claude-net")
    port_file = run_dir / f"mirror-agent-{os.getuid()}.port"
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    try:
        port = int(port_file.read_text().strip())
    except (OSError, ValueError):
        port = 0
    if port and proxy_control.probe(f"http://127.0.0.1:{port}/health"):
        return

    env = dict(os.environ)
    env.setdefault(
        "CLAUDE_NET_HUB", claude_json.hub_url() or _DEFAULT_HUB,
    )
    log = run_dir / f"mirror-agent-{os.getuid()}.log"
    try:
        with open(log, "ab") as logf:
            subprocess.Popen(
                [str(_MIRROR_AGENT)], stdin=subprocess.DEVNULL,
                stdout=logf, stderr=logf, start_new_session=True, env=env,
            )
    except OSError as exc:
        print(f"[claude-channels-v2] mirror agent: {exc}", file=sys.stderr)


def claude_channels_v2(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _start_mirror_agent()

    extra: list[str] = []
    if not any(a in _CHANNEL_FLAGS for a in argv):
        channels = claude_json.channel_args()
        if channels:
            extra = ["--dangerously-load-development-channels", *channels]

    # Tells the claude-net plugin that channels are known-loaded, so it
    # skips the LLM-driven self-test ceremony.
    os.environ["CLAUDE_NET_CHANNELS_PATCHED"] = "1"
    return claude_v2([*extra, *argv])


if __name__ == "__main__":
    sys.exit(claude_v2())
