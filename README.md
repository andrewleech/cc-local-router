# cc-local-router

Model-splitter patches and proxy for Claude Code: adds a custom model
alias (default `local`) to every model-name validation site in the
binary, and forces the availability gate to pass so the alias works
under a managed `availableModels` policy. A companion Bun proxy routes
`/v1/messages` requests for that alias to a local inference server and
everything else to `api.anthropic.com`.

Built as a provider package for
[`cc-patcher`](https://github.com/andrewleech/cc-patcher) — this repo
carries the patches and proxy, not the patching engine.

## Layout

- `cc_local_router/` — the `Patch`-shaped provider package
  (`model_alias.py`, `availability.py`), registered as a `cc_patcher`
  entry point.
- `proxy/index.ts` — the Bun/Elysia routing proxy.
- `bin/claude-v2` — wraps `cc-patcher launch` with the env vars the
  model picker and proxy need, and starts the proxy on demand.
- `bin/claude-channels-v2` — layers claude-net's channel MCP-arg
  injection and mirror-agent autostart on top of `claude-v2`. Only
  useful if `claude-net-patcher` (or another channel-patch provider)
  is also installed — the channel behaviour itself comes from that
  provider's patches, not from anything in this repo.
- `bin/claude-net-proxy-restart` — kill + restart the proxy, for use
  after editing `proxy/index.ts`.

## Install

Install `cc-patcher` as a uv tool with this package injected via
`--with` so both land in one environment and the entry point is
discovered:

```bash
uv tool install git+https://github.com/andrewleech/cc-patcher \
    --with git+https://github.com/andrewleech/cc-local-router
```

`cc-patcher launch` then produces a patched binary carrying the
model-alias patches. cc-patcher can host several providers in one
environment side by side; a later `uv tool upgrade cc-patcher` refreshes
them all without dropping any. See cc-patcher's README for the list of
supported provider plugins. (For local development, point the install
command at a working-tree path instead of the git URL.)

## Running

```bash
bun install                 # proxy deps (elysia)
ln -sf ~/cc-local-router/bin/claude-v2 ~/.local/bin/claude-v2
~/.local/bin/claude-v2 --version
```

`claude-v2` auto-starts the proxy (`bun --watch proxy/index.ts`) the
first time `ANTHROPIC_BASE_URL` points at the loopback default and
nothing is answering `/healthz` there yet. Env vars documented at the
top of `bin/claude-v2` control the alias name, upstream URLs, and
picker label; `CC_LOCAL_ROUTER_REPO` overrides the repo path if it's
not checked out at `~/cc-local-router`.
