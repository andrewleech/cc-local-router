"""Lifecycle management for the model-splitter proxy.

The proxy is `proxy/index.ts`, shipped as package data next to this
module and run directly by Bun -- it has no npm dependencies, so there
is no install step between `pip install cc-local-router` and a working
proxy. Bun itself is the only external requirement.

Backs the `cc-local-router-proxy` console script and the on-demand
start that `claude-v2` performs.
"""

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "http://127.0.0.1:8787"
STATE_DIRNAME = "cc-local-router"
_STARTUP_TIMEOUT = 10.0
_POLL_INTERVAL = 0.3


class ProxyError(Exception):
    pass


def entry_point() -> Path:
    """Path to the proxy's TypeScript entry point.

    `CC_LOCAL_ROUTER_PROXY` overrides it outright;
    `CC_LOCAL_ROUTER_REPO` points at a working tree, for running an
    edited copy against the installed package.
    """
    override = os.environ.get("CC_LOCAL_ROUTER_PROXY")
    if override:
        return Path(override).expanduser()
    repo = os.environ.get("CC_LOCAL_ROUTER_REPO")
    if repo:
        return Path(repo).expanduser() / "cc_local_router" / "proxy" / "index.ts"
    return Path(__file__).resolve().parent / "proxy" / "index.ts"


def base_url() -> str:
    return os.environ.get("ANTHROPIC_BASE_URL", DEFAULT_BASE_URL)


def is_loopback(url: str) -> bool:
    return url.startswith(("http://127.0.0.1:", "http://localhost:"))


def bind_env(url: str) -> dict[str, str]:
    """The host/port env the proxy must bind to in order to serve `url`.

    The proxy has its own defaults for these, so without pinning them
    from the URL the launcher can end up polling one port while the
    proxy listens on another.
    """
    parts = urllib.parse.urlsplit(url)
    env = {}
    if parts.hostname:
        env["CLAUDE_NET_PROXY_HOST"] = parts.hostname
    if parts.port:
        env["CLAUDE_NET_PROXY_PORT"] = str(parts.port)
    return env


def state_dir() -> Path:
    d = Path.home() / ".local" / "state" / STATE_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_path() -> Path:
    return state_dir() / "proxy.log"


def pid_path() -> Path:
    return state_dir() / "proxy.pid"


def probe(url: str, timeout: float = 1.0) -> bool:
    """True if `url` answers 200. Used for any local liveness check."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def is_healthy(url: str | None = None, timeout: float = 1.0) -> bool:
    """True if the proxy at `url` (default `base_url()`) is serving."""
    return probe((url or base_url()).rstrip("/") + "/healthz", timeout)


def running_pid() -> int | None:
    """PID from the state file, if that process is still alive."""
    try:
        pid = int(pid_path().read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def start(url: str | None = None) -> None:
    """Spawn the proxy detached and wait for it to answer /healthz."""
    target = url or base_url()
    entry = entry_point()
    if not entry.is_file():
        raise ProxyError(f"proxy entry point not found: {entry}")
    bun = shutil.which("bun")
    if bun is None:
        raise ProxyError(
            "bun is not on PATH; install it from https://bun.sh to run the "
            "model-splitter proxy"
        )

    cmd = [bun]
    if os.environ.get("CC_LOCAL_ROUTER_PROXY_WATCH"):
        # Reload on source edits -- only useful when running from a
        # working tree, so it is opt-in rather than the default.
        cmd.append("--watch")
    cmd.append(str(entry))

    log = log_path()
    with open(log, "ab") as logf:
        # start_new_session detaches the proxy into its own process
        # group, so a signal aimed at the launcher's group (say from
        # `timeout`) doesn't take the proxy down with it.
        proc = subprocess.Popen(
            cmd, cwd=entry.parent, stdin=subprocess.DEVNULL,
            stdout=logf, stderr=logf, start_new_session=True,
            env={**os.environ, **bind_env(target)},
        )
    pid_path().write_text(f"{proc.pid}\n")

    deadline = time.monotonic() + _STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if is_healthy(target):
            return
        if proc.poll() is not None:
            raise ProxyError(
                f"proxy exited immediately with code {proc.returncode} "
                f"(see {log})"
            )
        time.sleep(_POLL_INTERVAL)
    raise ProxyError(
        f"proxy did not answer {target}/healthz within "
        f"{_STARTUP_TIMEOUT:.0f}s (see {log})"
    )


def ensure_running(url: str | None = None) -> bool:
    """Start the proxy if `url` is a loopback address that isn't
    already serving. Returns True if the proxy is usable afterwards.

    A non-loopback base URL is left alone -- the user has pointed
    Claude Code somewhere deliberate.
    """
    target = url or base_url()
    if not is_loopback(target):
        return True
    if is_healthy(target):
        return True
    try:
        start(target)
    except ProxyError as exc:
        print(f"[cc-local-router] {exc}", file=sys.stderr)
        return False
    return True


def stop() -> bool:
    pid = running_pid()
    if pid is None:
        pid_path().unlink(missing_ok=True)
        return False
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        time.sleep(_POLL_INTERVAL)
        try:
            os.kill(pid, 0)
        except OSError:
            break
    else:
        os.kill(pid, signal.SIGKILL)
    pid_path().unlink(missing_ok=True)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cc-local-router-proxy",
        description="Manage the model-splitter proxy.",
    )
    parser.add_argument(
        "action", nargs="?", default="status",
        choices=["start", "stop", "restart", "status", "run", "where"],
        help=(
            "start/stop/restart the detached proxy, report status, run it "
            "in the foreground, or print the entry-point path"
        ),
    )
    args = parser.parse_args(argv)
    url = base_url()

    if args.action == "where":
        print(entry_point())
        return 0

    if args.action == "run":
        entry = entry_point()
        bun = shutil.which("bun")
        if bun is None:
            print("ERROR: bun is not on PATH", file=sys.stderr)
            return 1
        os.execve(bun, [bun, str(entry)], {**os.environ, **bind_env(url)})

    if args.action == "status":
        healthy = is_healthy(url)
        pid = running_pid()
        print(f"entry:   {entry_point()}")
        print(f"url:     {url}")
        print(f"healthy: {'yes' if healthy else 'no'}")
        print(f"pid:     {pid if pid is not None else '-'}")
        print(f"log:     {log_path()}")
        return 0 if healthy else 1

    if args.action in ("stop", "restart"):
        print("stopped" if stop() else "not running", flush=True)
        if args.action == "stop":
            return 0

    if args.action == "restart" and is_healthy(url):
        # Something not tracked by our pid file is holding the port.
        print(
            f"ERROR: {url} is still served by an untracked process",
            file=sys.stderr,
        )
        return 1

    if args.action == "start" and is_healthy(url):
        print("already running")
        return 0

    try:
        start(url)
    except ProxyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"started (pid {running_pid()}), serving {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
