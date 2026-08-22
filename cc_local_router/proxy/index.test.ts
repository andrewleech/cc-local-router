/**
 * Routing contract for the model-splitter proxy.
 *
 * Two throwaway upstreams stand in for the local inference server and
 * Anthropic; each echoes which one it is, so a request's destination is
 * observable. Run with `bun test` from the repo root.
 *
 * The env vars the proxy reads are captured at import time, so they are
 * set before the dynamic import below rather than inside a test.
 */

import { afterAll, beforeAll, expect, test } from "bun:test";

const local = Bun.serve({
  port: 0,
  fetch: (req) =>
    Response.json({ backend: "local", path: new URL(req.url).pathname }),
});
const anthropic = Bun.serve({
  port: 0,
  fetch: (req) =>
    Response.json({ backend: "anthropic", path: new URL(req.url).pathname }),
});

process.env.CLAUDE_PATCHER_MODEL_ALIAS = "local";
process.env.CLAUDE_NET_PROXY_LOCAL_URL = `http://127.0.0.1:${local.port}`;
process.env.CLAUDE_NET_PROXY_UPSTREAM = `http://127.0.0.1:${anthropic.port}`;

const { handle } = await import("./index.ts");

const proxy = Bun.serve({ port: 0, idleTimeout: 0, fetch: handle });
const base = `http://127.0.0.1:${proxy.port}`;

afterAll(() => {
  proxy.stop(true);
  local.stop(true);
  anthropic.stop(true);
});

async function messages(model: string) {
  const res = await fetch(`${base}/v1/messages`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ model, messages: [] }),
  });
  return { status: res.status, body: await res.json() };
}

test("healthz answers locally", async () => {
  const res = await fetch(`${base}/healthz`);
  expect(res.status).toBe(200);
  expect(await res.json()).toEqual({ status: "ok" });
});

test("root probe answers locally for GET and HEAD", async () => {
  const get = await fetch(`${base}/`);
  expect(get.status).toBe(200);
  const head = await fetch(`${base}/`, { method: "HEAD" });
  expect(head.status).toBe(200);
  expect(await head.text()).toBe("");
});

test("the alias routes to the local backend", async () => {
  const { status, body } = await messages("local");
  expect(status).toBe(200);
  expect(body.backend).toBe("local");
  expect(body.path).toBe("/v1/messages");
});

test("any other model routes to the default upstream", async () => {
  for (const model of ["sonnet", "claude-opus-5", ""]) {
    const { body } = await messages(model);
    expect(body.backend).toBe("anthropic");
  }
});

test("non-/v1/messages paths pass through to the default upstream", async () => {
  const res = await fetch(`${base}/v1/models`);
  expect((await res.json()).backend).toBe("anthropic");
});

test("count_tokens is not alias-routed", async () => {
  // It carries a model in the body but is not /v1/messages, so it must
  // still reach Anthropic -- the local engine has no such endpoint.
  const res = await fetch(`${base}/v1/messages/count_tokens`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ model: "local" }),
  });
  expect((await res.json()).backend).toBe("anthropic");
});

test("a malformed body is rejected without contacting an upstream", async () => {
  const res = await fetch(`${base}/v1/messages`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: "{not json",
  });
  expect(res.status).toBe(400);
  expect((await res.json()).error.type).toBe("invalid_request_error");
});

test("an unreachable upstream surfaces as a 502 api_error", async () => {
  const dead = Bun.serve({ port: 0, fetch: () => new Response("x") });
  const port = dead.port;
  dead.stop(true);
  process.env.CLAUDE_NET_PROXY_LOCAL_URL = `http://127.0.0.1:${port}`;
  // LOCAL_URL was captured at import time, so re-import in a fresh
  // registry to pick up the dead address.
  const mod = await import(`./index.ts?dead=${port}`);
  const isolated = Bun.serve({ port: 0, fetch: mod.handle });
  const res = await fetch(`http://127.0.0.1:${isolated.port}/v1/messages`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ model: "local" }),
  });
  expect(res.status).toBe(502);
  expect((await res.json()).error.type).toBe("api_error");
  isolated.stop(true);
});
