/**
 * claude-net model-splitter proxy.
 *
 * Sits between Claude Code and its upstreams. Inspects `body.model` on
 * POST /v1/messages and routes: matches the configured alias → local
 * inference server; anything else → Anthropic. Both upstreams speak
 * the Anthropic protocol natively (local via llama.cpp's Anthropic
 * adapter), so the proxy is a pure byte-stream reverse proxy — no
 * translation, no SSE reshaping.
 *
 * Kept as a hook point for response munging: if the local engine
 * later needs its structured-thinking blocks or tool-call shapes
 * normalised, that goes in `transformResponse()` below, currently a
 * no-op passthrough.
 *
 * Dependency-free on purpose: routing is a handful of path checks, so
 * this runs under `bun index.ts` with no `bun install` step. That is
 * what lets the Python package ship it as data and start it directly.
 *
 * Config, each read from the environment first, then from the `env`
 * block of ~/.claude/settings.json. The settings file is the better
 * home for per-machine backend details: it is where Claude Code already
 * looks, so the values stay put whether the proxy was started by a
 * launcher, by `cc-local-router-proxy`, or by hand.
 *
 *   CLAUDE_PATCHER_MODEL_ALIAS   model name that routes local (default "local")
 *                                — same var the patcher uses, so alias
 *                                stays defined in one place
 *   CLAUDE_NET_PROXY_LOCAL_URL   local backend URL (default http://127.0.0.1:8080)
 *   CLAUDE_NET_PROXY_LOCAL_MODEL served-model id to send to the local backend
 *                                in place of the alias. Unset forwards the
 *                                alias as-is; set it when the backend
 *                                validates `model` against its loaded
 *                                model's exact name.
 *   CLAUDE_NET_PROXY_UPSTREAM    default upstream (default https://api.anthropic.com)
 *   CLAUDE_NET_PROXY_HOST        bind host (default 127.0.0.1)
 *   CLAUDE_NET_PROXY_PORT        bind port (default 8787)
 */

import { execSync } from "node:child_process";
import { readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

// Build identifier — used by /version so `curl /version` shows exactly
// which source revision the running proxy came from. mtime is always
// available; git sha is best-effort. Computed at startup, so a
// `bun --watch` reload picks up the new values automatically.
const BUILD_INFO = (() => {
  const sourcePath = new URL(import.meta.url).pathname;
  let mtime: string;
  try {
    mtime = statSync(sourcePath).mtime.toISOString();
  } catch {
    mtime = "unknown";
  }
  let gitSha: string | null = null;
  try {
    gitSha = execSync("git rev-parse --short HEAD", {
      cwd: new URL(".", import.meta.url).pathname,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
  } catch {
    /* not in a git tree or git absent */
  }
  let gitDirty = false;
  if (gitSha) {
    try {
      const s = execSync("git status --porcelain -- .", {
        cwd: new URL(".", import.meta.url).pathname,
        encoding: "utf8",
        stdio: ["ignore", "pipe", "ignore"],
      }).trim();
      gitDirty = s.length > 0;
    } catch {}
  }
  return {
    source: sourcePath,
    mtime,
    git_sha: gitSha,
    git_dirty: gitDirty,
    started_at: new Date().toISOString(),
    pid: process.pid,
  };
})();

const SETTINGS_PATH = join(homedir(), ".claude", "settings.json");

function settingsEnv(key: string): string | undefined {
  try {
    const parsed = JSON.parse(readFileSync(SETTINGS_PATH, "utf8"));
    const val = parsed?.env?.[key];
    return typeof val === "string" && val !== "" ? val : undefined;
  } catch {
    return undefined;
  }
}

// Environment first, then ~/.claude/settings.json, then the default.
function conf(key: string): string | undefined;
function conf(key: string, fallback: string): string;
function conf(key: string, fallback?: string): string | undefined {
  return process.env[key] || settingsEnv(key) || fallback;
}

const ALIAS = conf("CLAUDE_PATCHER_MODEL_ALIAS", "local");
const LOCAL_URL = conf(
  "CLAUDE_NET_PROXY_LOCAL_URL", "http://127.0.0.1:8080",
).replace(/\/$/, "");
const DEFAULT_UPSTREAM = conf(
  "CLAUDE_NET_PROXY_UPSTREAM", "https://api.anthropic.com",
).replace(/\/$/, "");
// Unset means "forward the alias unchanged".
const LOCAL_MODEL = conf("CLAUDE_NET_PROXY_LOCAL_MODEL");

/** A selectable model backed by some upstream.
 *
 *  `id` is what Claude Code sees and sends. It must match
 *  /(claude|anthropic)/i or Claude Code's gateway discovery filters it
 *  out, hence the `anthropic.` prefix convention.
 *  `model` is the id the backend itself wants, when that differs. */
interface ModelOption {
  id: string;
  display_name?: string;
  url: string;
  model?: string;
}

function parseModels(raw: string | undefined): ModelOption[] {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) throw new Error("not an array");
    return parsed.filter((m) => typeof m?.id === "string" && typeof m?.url === "string");
  } catch (err) {
    log("error", "bad_models_config", {
      var: "CLAUDE_NET_PROXY_MODELS",
      error: String(err),
    });
    return [];
  }
}

const MODELS = parseModels(conf("CLAUDE_NET_PROXY_MODELS"));
// Anthropic's /v1/models entries carry a created_at; Claude Code only
// reads id and display_name, so a fixed value keeps the response shape
// valid without inventing per-model dates.
const MODEL_CREATED_AT = "2025-01-01T00:00:00Z";
const HOST = conf("CLAUDE_NET_PROXY_HOST", "127.0.0.1");
const PORT = Number(conf("CLAUDE_NET_PROXY_PORT", "8787"));

interface RouteDecision {
  upstream: string;
  backendLabel: string;
  /** The model id to send upstream, which is not always the one the
   *  client asked for -- see LOCAL_MODEL. */
  model: string;
}

function pickBackend(model: string): RouteDecision {
  const option = MODELS.find((m) => m.id === model);
  if (option) {
    return {
      upstream: option.url.replace(/\/$/, ""),
      backendLabel: `option:${option.id}`,
      model: option.model ?? option.id,
    };
  }
  if (model === ALIAS) {
    return {
      upstream: LOCAL_URL,
      backendLabel: "local",
      model: LOCAL_MODEL ?? model,
    };
  }
  return { upstream: DEFAULT_UPSTREAM, backendLabel: "anthropic", model };
}

function stripHopHeaders(h: Headers): Headers {
  const out = new Headers(h);
  // Hop-by-hop / connection-scoped headers that must not be forwarded.
  // content-length is dropped so the runtime recomputes from the body.
  for (const name of [
    "host",
    "connection",
    "content-length",
    "transfer-encoding",
    "keep-alive",
    "proxy-authorization",
    "proxy-connection",
    "upgrade",
    "te",
  ]) {
    out.delete(name);
  }
  return out;
}

function stripResponseHopHeaders(h: Headers): Headers {
  const out = new Headers(h);
  // Bun's fetch transparently decompresses response bodies but keeps
  // the original content-encoding + content-length headers, which
  // makes downstream clients try to decompress plaintext (ZlibError).
  // Drop both. connection is also hop-scoped.
  for (const name of [
    "content-encoding",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
  ]) {
    out.delete(name);
  }
  return out;
}

function instrumentedBody(
  response: Response,
  decision: RouteDecision,
  requestId: string,
  started: number,
): ReadableStream<Uint8Array> | null {
  // Identity passthrough via TransformStream. Compared to the previous
  // eager-start ReadableStream, this pattern gives Bun's HTTP server
  // full control over pull() cadence and preserves per-chunk arrival
  // timing to the client without a hop through an internal queue we
  // mediate.
  //
  // Also logs every chunk-in / chunk-out with size + time-since-
  // last-chunk so we can see exactly where the flow stalls if the
  // stream ever stops mid-response.
  if (!response.body) return null;
  let bytesIn = 0;
  let bytesOut = 0;
  let chunksIn = 0;
  let chunksOut = 0;
  let lastChunkAt = Date.now();

  const transform = new TransformStream<Uint8Array, Uint8Array>({
    transform(chunk, controller) {
      const now = Date.now();
      const gap = now - lastChunkAt;
      lastChunkAt = now;
      chunksIn++;
      bytesIn += chunk.byteLength;
      // Log every chunk >100ms gap and every 10th otherwise, so
      // long silences show up but tight streaming doesn't spam.
      if (gap > 100 || chunksIn <= 5 || chunksIn % 10 === 0) {
        log("info", "chunk", {
          request_id: requestId,
          n: chunksIn,
          size: chunk.byteLength,
          gap_ms: gap,
          total_bytes: bytesIn,
          elapsed_ms: now - started,
        });
      }
      controller.enqueue(chunk);
      chunksOut++;
      bytesOut += chunk.byteLength;
    },
    flush() {
      log("info", "stream_end", {
        request_id: requestId,
        backend: decision.backendLabel,
        status: response.status,
        chunks_in: chunksIn,
        chunks_out: chunksOut,
        bytes_in: bytesIn,
        bytes_out: bytesOut,
        elapsed_ms: Date.now() - started,
      });
    },
  });

  response.body.pipeTo(transform.writable).catch((err) => {
    log("error", "upstream_body_error", {
      request_id: requestId,
      backend: decision.backendLabel,
      chunks_in: chunksIn,
      bytes_in: bytesIn,
      elapsed_ms: Date.now() - started,
      error: String(err),
    });
  });

  return transform.readable;
}

async function transformResponse(
  response: Response,
  decision: RouteDecision,
  requestId: string,
  started: number,
): Promise<Response> {
  // Passthrough for now, minus hop headers that would confuse the
  // client. If the local engine's tool-call shape or structured-
  // thinking blocks drift from what Claude Code expects, intercept +
  // rewrite the SSE stream here — decision.backendLabel identifies
  // which path we're on.
  const body = instrumentedBody(response, decision, requestId, started);
  return new Response(body, {
    status: response.status,
    statusText: response.statusText,
    headers: stripResponseHopHeaders(response.headers),
  });
}

async function forward(
  request: Request,
  pathAndSearch: string,
  upstream: string,
  body: BodyInit | undefined,
): Promise<Response> {
  const target = upstream + pathAndSearch;
  const headers = stripHopHeaders(request.headers);
  return fetch(target, {
    method: request.method,
    headers,
    body,
    // Bun's fetch needs an explicit duplex when streaming a request body.
    // @ts-expect-error — 'duplex' is a valid RequestInit option in Bun
    duplex: body instanceof ReadableStream ? "half" : undefined,
    redirect: "manual",
  });
}

function log(
  level: "info" | "warn" | "error",
  msg: string,
  fields: Record<string, unknown> = {},
): void {
  const entry = {
    ts: new Date().toISOString(),
    level,
    msg,
    ...fields,
  };
  // stderr so it doesn't collide with anything writing to stdout
  process.stderr.write(`${JSON.stringify(entry)}\n`);
}

// Root-path connectivity probe. Claude Code's embedded Bun fetch does
// a HEAD / on ANTHROPIC_BASE_URL before every real request to verify
// reachability. It doesn't look at the body or status, just needs a
// response. Answering locally saves the ~290 ms round-trip to
// Anthropic per session start and removes a startup dependency on
// Anthropic being reachable when the user is only using the local
// backend.
const rootProbeResponse = (method: string) =>
  new Response(
    method === "HEAD"
      ? null
      : JSON.stringify({
          service: "claude-net-proxy",
          msg: "OK — POST /v1/messages for routed traffic",
        }),
    {
      status: 200,
      headers: { "content-type": "application/json" },
    },
  );

function errorResponse(
  status: number,
  type: string,
  message: string,
): Response {
  return new Response(
    JSON.stringify({ type: "error", error: { type, message } }),
    { status, headers: { "content-type": "application/json" } },
  );
}

async function handleMessages(request: Request, url: URL): Promise<Response> {
  const started = Date.now();
  const requestId = Math.random().toString(36).slice(2, 10);
  let model = "";
  let bodyText = "";
  let parsed: Record<string, unknown> | undefined;
  try {
    bodyText = await request.text();
    parsed = JSON.parse(bodyText);
    model = typeof parsed?.model === "string" ? parsed.model : "";
  } catch (err) {
    log("warn", "invalid_json_body", { error: String(err) });
    return errorResponse(
      400,
      "invalid_request_error",
      "invalid JSON in request body",
    );
  }

  const decision = pickBackend(model);
  log("info", "route", {
    request_id: requestId,
    path: url.pathname,
    model,
    routed_model: decision.model,
    backend: decision.backendLabel,
    upstream: decision.upstream,
  });

  // Rewrite the outgoing `model` when the backend's served-model id
  // differs from the alias Claude Code sends. llama.cpp and friends
  // validate `model` against what they actually have loaded, and the
  // patched picker can only ever send the alias.
  const outgoingBody =
    decision.model !== model && parsed
      ? JSON.stringify({ ...parsed, model: decision.model })
      : bodyText;

  let upstreamResp: Response;
  try {
    upstreamResp = await forward(
      request,
      url.pathname + url.search,
      decision.upstream,
      outgoingBody,
    );
  } catch (err) {
    log("error", "upstream_unreachable", {
      backend: decision.backendLabel,
      upstream: decision.upstream,
      error: String(err),
    });
    return errorResponse(
      502,
      "api_error",
      `upstream unreachable: ${String(err)}`,
    );
  }

  log("info", "upstream_status", {
    request_id: requestId,
    backend: decision.backendLabel,
    status: upstreamResp.status,
    elapsed_ms: Date.now() - started,
  });

  return await transformResponse(upstreamResp, decision, requestId, started);
}

// Everything other than POST /v1/messages — /v1/models,
// /v1/messages/count_tokens, etc. — goes to the default upstream.
// Model-name routing only applies to /v1/messages.
async function handlePassthrough(
  request: Request,
  url: URL,
): Promise<Response> {
  const started = Date.now();
  const requestId = Math.random().toString(36).slice(2, 10);
  try {
    const upstream = await forward(
      request,
      url.pathname + url.search,
      DEFAULT_UPSTREAM,
      request.body ?? undefined,
    );
    log("info", "passthrough", {
      request_id: requestId,
      method: request.method,
      path: url.pathname,
      status: upstream.status,
    });
    return await transformResponse(
      upstream,
      { upstream: DEFAULT_UPSTREAM, backendLabel: "anthropic", model: "" },
      requestId,
      started,
    );
  } catch (err) {
    log("error", "passthrough_unreachable", {
      path: url.pathname,
      error: String(err),
    });
    return new Response("upstream unreachable", { status: 502 });
  }
}

// GET /v1/models is how Claude Code discovers extra selectable models:
// with CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY set it fetches this
// list from ANTHROPIC_BASE_URL and adds each entry to the /model picker,
// labelled from display_name. Our options are appended to whatever the
// default upstream reports so the built-in models keep showing up.
async function handleModels(request: Request, url: URL): Promise<Response> {
  const mine = MODELS.map((m) => ({
    type: "model",
    id: m.id,
    display_name: m.display_name ?? m.id,
    created_at: MODEL_CREATED_AT,
  }));

  let upstreamData: unknown[] = [];
  let upstreamStatus = 0;
  try {
    const resp = await forward(
      request, url.pathname + url.search, DEFAULT_UPSTREAM, undefined,
    );
    upstreamStatus = resp.status;
    if (resp.ok) {
      const body = await resp.json();
      if (Array.isArray(body?.data)) upstreamData = body.data;
    }
  } catch (err) {
    log("warn", "models_upstream_unreachable", { error: String(err) });
  }

  const seen = new Set(mine.map((m) => m.id));
  const merged = [
    ...upstreamData.filter(
      (m: any) => typeof m?.id === "string" && !seen.has(m.id),
    ),
    ...mine,
  ];
  log("info", "models", {
    upstream_status: upstreamStatus,
    upstream_count: upstreamData.length,
    option_count: mine.length,
  });
  return Response.json({
    data: merged,
    has_more: false,
    first_id: merged[0] ? (merged[0] as any).id : null,
    last_id: merged.length ? (merged[merged.length - 1] as any).id : null,
  });
}

export async function handle(request: Request): Promise<Response> {
  const url = new URL(request.url);
  const path = url.pathname;
  if (request.method === "GET" && path === "/v1/models" && MODELS.length) {
    return handleModels(request, url);
  }
  if (request.method === "GET" && path === "/healthz") {
    return Response.json({ status: "ok" });
  }
  if (request.method === "GET" && path === "/version") {
    return Response.json(BUILD_INFO);
  }
  if (path === "/" && (request.method === "GET" || request.method === "HEAD")) {
    return rootProbeResponse(request.method);
  }
  if (request.method === "POST" && path === "/v1/messages") {
    return handleMessages(request, url);
  }
  return handlePassthrough(request, url);
}

if (import.meta.main) {
  // idleTimeout=0 disables Bun.serve's socket-idle killer. Default (10s)
  // tears down streaming responses when Anthropic pauses for extended
  // thinking — no bytes flow to the client for 30-60s and the socket
  // closes, which the client reports as "Connection closed mid-response".
  // Max value is 255s, 0 disables entirely.
  const server = Bun.serve({
    hostname: HOST,
    port: PORT,
    idleTimeout: 0,
    fetch: handle,
  });
  log("info", "proxy_started", {
    bind: `${HOST}:${PORT}`,
    alias: ALIAS,
    local_url: LOCAL_URL,
    local_model: LOCAL_MODEL ?? null,
    default_upstream: DEFAULT_UPSTREAM,
    build: BUILD_INFO,
  });
  const shutdown = () => {
    log("info", "proxy_shutting_down", {});
    server.stop();
    process.exit(0);
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
}
