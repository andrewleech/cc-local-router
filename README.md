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

Everything ships inside the one Python package, so installing it gives
you the patches, the proxy and the launchers together:

- `model_alias.py`, `availability.py` — the `Patch`-shaped provider,
  registered as a `cc_patcher` entry point.
- `proxy/index.ts` — the routing proxy. Dependency-free, so Bun runs it
  in place with no `bun install`; that is what lets it ship as package
  data.
- `launcher.py` — the `claude-v2` and `claude-channels-v2` console
  scripts. `claude-v2` sets the env the picker and proxy need, starts
  the proxy on demand, and execs the patched binary.
  `claude-channels-v2` adds claude-net's per-MCP-server channel args and
  mirror-agent autostart; that part is only useful alongside
  `claude-net-patcher`, whose patches provide the channel behaviour
  itself.
- `proxy_control.py` — the `cc-local-router-proxy` console script:
  `start`, `stop`, `restart`, `status`, `run`, `where`.
- `claude_json.py` — best-effort readers for `~/.claude.json`.

## Install

Install `cc-patcher` as a uv tool with this package injected via
`--with` so both land in one environment and the entry point is
discovered:

```bash
uv tool install git+https://github.com/andrewleech/cc-patcher \
    --with git+https://github.com/andrewleech/cc-local-router \
    --with-executables-from cc-local-router
```

`--with-executables-from` is what puts `claude-v2`,
`claude-channels-v2` and `cc-local-router-proxy` on `PATH`; without it
uv installs only the primary package's executables and you get the
patches but no launchers.

cc-patcher can host several providers in one environment side by side, so
add `claude-net-patcher` the same way for channel support:

```bash
uv tool install git+https://github.com/andrewleech/cc-patcher \
    --with "git+https://github.com/andrewleech/claude-net#subdirectory=patcher-ext" \
    --with git+https://github.com/andrewleech/cc-local-router \
    --with-executables-from cc-local-router
```

A later `uv tool upgrade cc-patcher` refreshes every provider without
dropping any. (For local development, point the install command at a
working-tree path instead of the git URL.)

Bun is the only non-Python requirement — the proxy runs under it
directly. There is no `bun install` step and no repo checkout needed.

## Running

```bash
claude-v2 --version
```

`claude-v2` auto-starts the proxy the first time `ANTHROPIC_BASE_URL`
points at a loopback address and nothing answers `/healthz` there. A
non-loopback base URL is left alone, on the assumption you pointed it
somewhere deliberately.

```bash
cc-local-router-proxy status     # entry point, URL, health, pid, log
cc-local-router-proxy restart    # after editing proxy/index.ts
cc-local-router-proxy run        # foreground, for debugging
cc-local-router-proxy where      # path of the index.ts actually in use
```

The proxy binds the host and port parsed out of `ANTHROPIC_BASE_URL`, so
the launcher can't end up polling one port while the proxy listens on
another.

### The local backend must speak the Anthropic Messages API

The proxy is a pure byte-stream reverse proxy — it does not translate
between API shapes. The backend behind `CLAUDE_NET_PROXY_LOCAL_URL` must
serve `POST /v1/messages` in Anthropic format itself. Several local
servers now do, so no translation layer is needed:

- **llama.cpp / llama-server** — native since
  [PR #17570](https://github.com/ggml-org/llama.cpp/pull/17570);
  streaming, tool use (`--jinja`), and `count_tokens`.
- **ollama** — v0.14+, but no `count_tokens` endpoint.
- **vLLM**, **LM Studio** (0.4.1+), **llamafile**.

An OpenAI-compatible-only backend will not work as-is.

## Configuration

Every setting is read from the environment first, then from the `env`
block of `~/.claude/settings.json`, then a built-in default. Putting
per-machine backend details in `settings.json` is usually better: Claude
Code already reads that file, and the launchers deliberately consult it
before exporting their own defaults so they can't override you.

```json
{
  "env": {
    "CLAUDE_NET_PROXY_LOCAL_URL": "http://titan:8080",
    "CLAUDE_NET_PROXY_LOCAL_MODEL": "qwen3.8-27b",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "qwen3.8-27b",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION": "Local inference server (titan)"
  }
}
```

| Variable | Default | Effect |
| --- | --- | --- |
| `CLAUDE_PATCHER_MODEL_ALIAS` | `local` | alias that routes to the local backend; also the name the patches insert |
| `ANTHROPIC_BASE_URL` | `http://127.0.0.1:8787` | where Claude Code sends traffic, and what the proxy binds |
| `CLAUDE_NET_PROXY_LOCAL_URL` | `http://127.0.0.1:8080` | the local inference server |
| `CLAUDE_NET_PROXY_LOCAL_MODEL` | — | served-model id to send in place of the alias. Needed when the backend validates `model` against its loaded model's exact name, which llama.cpp does — the patched picker can only ever send the alias |
| `CLAUDE_NET_PROXY_UPSTREAM` | `https://api.anthropic.com` | everything that isn't the alias |
| `CC_LOCAL_ROUTER_REPO` | — | run the proxy from a working tree instead of the installed copy |
| `CC_LOCAL_ROUTER_PROXY` | — | run a specific `index.ts` |
| `CC_LOCAL_ROUTER_PROXY_WATCH` | unset | pass `--watch` to Bun, for editing the proxy in place |
| `CLAUDE_V2_FORCE_API_KEY` | — | use an API key instead of the OAuth login (a stray `ANTHROPIC_API_KEY` is otherwise unset, so it can't silently hijack subscription auth) |

## Tests

```bash
uv run --with pytest pytest tests/    # patches, config readers, proxy control
bun test cc_local_router/proxy/       # proxy routing contract
```

## Why patch the binary instead of wrapping it

Claude Code only lets you select a model at two places: a `model:`
field (subagent frontmatter, `--model`, the `Workflow` tool's `model`
option) and `agentType` (subagent type). Both are resolved against
lists baked into the binary — the zod enum on the Agent tool, the
alias allowlists, the CLI/TUI pickers, and the resolver switch that
turns a model name into an API model id. A name that isn't in those
lists doesn't exist as a model, full stop.

An MCP server can add tools, but it can't add entries to those lists.
Wrapping another inference backend behind an MCP tool makes it
callable — a peer of `Read` or `Bash` — but not selectable as a model
or subagent type. It can't be named in a subagent's `model:` field,
passed as `agentType` to a dynamic workflow's `agent()` calls, or
picked from the CLI/TUI model picker. At best you get a relay
subagent that calls the tool and passes the text back, still driven by
a real Claude model doing the tool-calling.

Patching adds the alias directly into those lists (`model_alias.py`)
and forces the availability gate open (`availability.py`), so the
alias becomes a real model id everywhere Claude Code accepts one. The
proxy then intercepts requests for that id and routes them to whatever
actually serves it. This is what makes the alias usable natively by
subagents, `Task`/`Agent` dispatch, and `Workflow`'s `agent()`/
`parallel()`/`pipeline()` orchestration, with no relay hop and no
loss of the model's own tool-use loop.
