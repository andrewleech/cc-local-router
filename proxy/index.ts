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
 * Config (all env vars):
 *   CLAUDE_PATCHER_MODEL_ALIAS   model name that routes local (default "local")
 *                                — same var the patcher uses, so alias
 *                                stays defined in one place
 *   CLAUDE_NET_PROXY_LOCAL_URL   local backend URL (default http://127.0.0.1:8080)
 *   CLAUDE_NET_PROXY_UPSTREAM    default upstream (default https://api.anthropic.com)
 *   CLAUDE_NET_PROXY_HOST        bind host (default 127.0.0.1)
 *   CLAUDE_NET_PROXY_PORT        bind port (default 8787)
 */

import { execSync } from "node:child_process";
import { statSync } from "node:fs";
import { Elysia } from "elysia";

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

const ALIAS = process.env.CLAUDE_PATCHER_MODEL_ALIAS ?? "local";
const LOCAL_URL = (
  process.env.CLAUDE_NET_PROXY_LOCAL_URL ?? "http://127.0.0.1:8080"
).replace(/\/$/, "");
const DEFAULT_UPSTREAM = (
  process.env.CLAUDE_NET_PROXY_UPSTREAM ?? "https://api.anthropic.com"
).replace(/\/$/, "");
const HOST = process.env.CLAUDE_NET_PROXY_HOST ?? "127.0.0.1";
const PORT = Number(process.env.CLAUDE_NET_PROXY_PORT ?? 8787);

interface RouteDecision {
  upstream: string;
  backendLabel: string;
  model: string;
}

function pickBackend(model: string): RouteDecision {
  if (model === ALIAS) {
    return { upstream: LOCAL_URL, backendLabel: "local", model };
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

export function createProxyApp(): Elysia {
  return (
    new Elysia()
      .get("/healthz", () => ({ status: "ok" }))
      .get("/version", () => BUILD_INFO)
      .head("/", ({ request }) => rootProbeResponse(request.method))
      .get("/", ({ request }) => rootProbeResponse(request.method))
      .post("/v1/messages", async ({ request }) => {
        const url = new URL(request.url);
        const started = Date.now();
        const requestId = Math.random().toString(36).slice(2, 10);
        let model = "";
        let bodyText = "";
        try {
          bodyText = await request.text();
          const parsed = JSON.parse(bodyText);
          model = typeof parsed.model === "string" ? parsed.model : "";
        } catch (err) {
          log("warn", "invalid_json_body", {
            error: String(err),
          });
          return new Response(
            JSON.stringify({
              type: "error",
              error: {
                type: "invalid_request_error",
                message: "invalid JSON in request body",
              },
            }),
            {
              status: 400,
              headers: { "content-type": "application/json" },
            },
          );
        }

        const decision = pickBackend(model);
        log("info", "route", {
          request_id: requestId,
          path: url.pathname,
          model,
          backend: decision.backendLabel,
          upstream: decision.upstream,
        });

        let upstreamResp: Response;
        try {
          upstreamResp = await forward(
            request,
            url.pathname + url.search,
            decision.upstream,
            bodyText,
          );
        } catch (err) {
          log("error", "upstream_unreachable", {
            backend: decision.backendLabel,
            upstream: decision.upstream,
            error: String(err),
          });
          return new Response(
            JSON.stringify({
              type: "error",
              error: {
                type: "api_error",
                message: `upstream unreachable: ${String(err)}`,
              },
            }),
            {
              status: 502,
              headers: { "content-type": "application/json" },
            },
          );
        }

        log("info", "upstream_status", {
          request_id: requestId,
          backend: decision.backendLabel,
          status: upstreamResp.status,
          elapsed_ms: Date.now() - started,
        });

        return await transformResponse(
          upstreamResp,
          decision,
          requestId,
          started,
        );
      })
      // Everything else — /v1/models, /v1/messages/count_tokens, etc. —
      // falls through to the default upstream (Anthropic). Model-name
      // routing only applies to /v1/messages, which is where it matters.
      // Everything else — /v1/models, /v1/messages/count_tokens, etc.
      // — falls through to the default upstream (Anthropic).
      // Model-name routing only applies to /v1/messages.
      .all("*", async ({ request }) => {
        const url = new URL(request.url);
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
            {
              upstream: DEFAULT_UPSTREAM,
              backendLabel: "anthropic",
              model: "",
            },
            requestId,
            started,
          );
        } catch (err) {
          log("error", "passthrough_unreachable", {
            path: url.pathname,
            error: String(err),
          });
          return new Response("upstream unreachable", {
            status: 502,
          });
        }
      })
  );
}

if (import.meta.main) {
  const app = createProxyApp();
  // idleTimeout=0 disables Bun.serve's socket-idle killer. Default (10s)
  // tears down streaming responses when Anthropic pauses for extended
  // thinking — no bytes flow to the client for 30-60s and the socket
  // closes, which the client reports as "Connection closed mid-response".
  // Max value is 255s, 0 disables entirely.
  app.listen({ hostname: HOST, port: PORT, idleTimeout: 0 });
  log("info", "proxy_started", {
    bind: `${HOST}:${PORT}`,
    alias: ALIAS,
    local_url: LOCAL_URL,
    default_upstream: DEFAULT_UPSTREAM,
    build: BUILD_INFO,
  });
  const shutdown = () => {
    log("info", "proxy_shutting_down", {});
    app.stop();
    process.exit(0);
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
}
