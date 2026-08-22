"""Readers for Claude Code's own config: `~/.claude.json` for MCP
servers and API-key approvals, `~/.claude/settings.json` for the `env`
block.

Every accessor is best-effort: a missing, unreadable or unexpected
config yields an empty result rather than raising, because none of the
launcher behaviour these feed is essential to starting Claude Code.
"""

import json
import os
from pathlib import Path
from typing import Any

CONFIG_NAME = ".claude.json"


def config_path() -> Path:
    return Path.home() / CONFIG_NAME


def settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _load_file(path: Path) -> dict[str, Any]:
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _load() -> dict[str, Any]:
    return _load_file(config_path())


def settings_env(key: str) -> str | None:
    """`key` from the `env` block of ~/.claude/settings.json.

    Claude Code applies that block to its own environment at startup,
    so a launcher that exports its own default for the same variable
    would silently win over the user's settings. Consulting the file
    first keeps settings.json authoritative.
    """
    env = _load_file(settings_path()).get("env")
    if not isinstance(env, dict):
        return None
    val = env.get(key)
    return val if isinstance(val, str) and val else None


def _mcp_servers(scope: Any) -> dict[str, Any]:
    if not isinstance(scope, dict):
        return {}
    servers = scope.get("mcpServers")
    return servers if isinstance(servers, dict) else {}


def channel_args(cwd: str | os.PathLike[str] | None = None) -> list[str]:
    """`server:<name>` for every MCP server visible from `cwd` --
    globals first, then the entry for this project only.

    These become the arguments to
    `--dangerously-load-development-channels`, which is per-server.
    """
    data = _load()
    cwd = str(cwd if cwd is not None else Path.cwd())
    names: list[str] = []
    seen: set[str] = set()

    def add(scope: Any) -> None:
        for name in _mcp_servers(scope):
            if name not in seen:
                seen.add(name)
                names.append(f"server:{name}")

    add(data)
    projects = data.get("projects")
    if isinstance(projects, dict):
        add(projects.get(cwd))
    return names


def hub_url() -> str | None:
    """`CLAUDE_NET_HUB` from the claude-net MCP server's env block, in
    whichever scope defines it first."""
    data = _load()
    scopes: list[Any] = [data]
    projects = data.get("projects")
    if isinstance(projects, dict):
        scopes.extend(projects.values())
    for scope in scopes:
        cfg = _mcp_servers(scope).get("claude-net")
        if not isinstance(cfg, dict):
            continue
        env = cfg.get("env")
        if isinstance(env, dict) and env.get("CLAUDE_NET_HUB"):
            return str(env["CLAUDE_NET_HUB"])
    return None


def approve_api_key(key: str) -> None:
    """Pre-approve `key` so Claude Code doesn't prompt for it.

    Claude Code stores the last 20 characters of each key it has asked
    about under `customApiKeyResponses`. Writes are atomic via a
    temp-file rename so a crash can't truncate the user's config.
    """
    if not key:
        return
    path = config_path()
    data = _load()
    if not data:
        return
    short = key[-20:]
    responses = data.setdefault("customApiKeyResponses", {})
    approved = responses.setdefault("approved", [])
    rejected = responses.setdefault("rejected", [])
    if not isinstance(approved, list) or not isinstance(rejected, list):
        return
    changed = False
    if short not in approved:
        approved.append(short)
        changed = True
    if short in rejected:
        rejected.remove(short)
        changed = True
    if not changed:
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
