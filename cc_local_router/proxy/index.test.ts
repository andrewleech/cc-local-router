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

test("the served-model id replaces the alias when configured", async () => {
  // llama.cpp validates `model` against what it has loaded, but the
  // patched picker can only ever send the alias.
  const seen: unknown[] = [];
  const backend = Bun.serve({
    port: 0,
    fetch: async (req) => {
      seen.push((await req.json()).model);
      return Response.json({ ok: true });
    },
  });
  process.env.CLAUDE_NET_PROXY_LOCAL_URL = `http://127.0.0.1:${backend.port}`;
  process.env.CLAUDE_NET_PROXY_LOCAL_MODEL = "qwen3.8-27b";
  const mod = await import(`./index.ts?rewrite=${backend.port}`);
  const isolated = Bun.serve({ port: 0, fetch: mod.handle });

  await fetch(`http://127.0.0.1:${isolated.port}/v1/messages`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ model: "local", max_tokens: 8, messages: [] }),
  });
  expect(seen).toEqual(["qwen3.8-27b"]);

  // Non-alias traffic keeps its own model name.
  await fetch(`http://127.0.0.1:${isolated.port}/v1/messages`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ model: "sonnet", messages: [] }),
  });
  expect(seen).toEqual(["qwen3.8-27b"]);

  delete process.env.CLAUDE_NET_PROXY_LOCAL_MODEL;
  isolated.stop(true);
  backend.stop(true);
});

test("other body fields survive the model rewrite", async () => {
  let body: Record<string, unknown> = {};
  const backend = Bun.serve({
    port: 0,
    fetch: async (req) => {
      body = await req.json();
      return Response.json({ ok: true });
    },
  });
  process.env.CLAUDE_NET_PROXY_LOCAL_URL = `http://127.0.0.1:${backend.port}`;
  process.env.CLAUDE_NET_PROXY_LOCAL_MODEL = "served-id";
  const mod = await import(`./index.ts?keep=${backend.port}`);
  const isolated = Bun.serve({ port: 0, fetch: mod.handle });
  await fetch(`http://127.0.0.1:${isolated.port}/v1/messages`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      model: "local",
      max_tokens: 99,
      system: "sys",
      stream: true,
      messages: [{ role: "user", content: "hi" }],
    }),
  });
  expect(body.model).toBe("served-id");
  expect(body.max_tokens).toBe(99);
  expect(body.system).toBe("sys");
  expect(body.stream).toBe(true);
  expect(body.messages).toEqual([{ role: "user", content: "hi" }]);

  delete process.env.CLAUDE_NET_PROXY_LOCAL_MODEL;
  isolated.stop(true);
  backend.stop(true);
});

test("configured options are served from /v1/models and routed", async () => {
  // Claude Code's gateway discovery GETs this list and adds each entry
  // to the picker. Ids must match /(claude|anthropic)/i or it drops
  // them, hence the anthropic. prefix.
  const backend = Bun.serve({
    port: 0,
    fetch: async (req) =>
      Response.json({ backend: "option", model: (await req.json()).model }),
  });
  const upstream = Bun.serve({
    port: 0,
    fetch: () =>
      Response.json({ data: [{ type: "model", id: "claude-opus-5" }] }),
  });
  process.env.CLAUDE_NET_PROXY_UPSTREAM = `http://127.0.0.1:${upstream.port}`;
  process.env.CLAUDE_NET_PROXY_MODELS = JSON.stringify([
    {
      id: "anthropic.qwen",
      display_name: "Qwen (titan)",
      url: `http://127.0.0.1:${backend.port}`,
      model: "qwen3.8-27b",
    },
  ]);
  const mod = await import(`./index.ts?models=${backend.port}`);
  const iso = Bun.serve({ port: 0, fetch: mod.handle });
  const at = `http://127.0.0.1:${iso.port}`;

  const listed = await (await fetch(`${at}/v1/models`)).json();
  const ids = listed.data.map((m: any) => m.id);
  expect(ids).toContain("anthropic.qwen");
  expect(ids).toContain("claude-opus-5"); // upstream entries survive
  const mine = listed.data.find((m: any) => m.id === "anthropic.qwen");
  expect(mine.display_name).toBe("Qwen (titan)");
  expect(mine.type).toBe("model");

  // Selecting it routes to that option's backend, with its served id.
  const res = await (await fetch(`${at}/v1/messages`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ model: "anthropic.qwen", messages: [] }),
  })).json();
  expect(res).toEqual({ backend: "option", model: "qwen3.8-27b" });

  delete process.env.CLAUDE_NET_PROXY_MODELS;
  delete process.env.CLAUDE_NET_PROXY_UPSTREAM;
  iso.stop(true);
  backend.stop(true);
  upstream.stop(true);
});

test("a malformed models config is ignored rather than fatal", async () => {
  const upstream = Bun.serve({
    port: 0,
    fetch: () => Response.json({ backend: "anthropic" }),
  });
  process.env.CLAUDE_NET_PROXY_UPSTREAM = `http://127.0.0.1:${upstream.port}`;
  process.env.CLAUDE_NET_PROXY_MODELS = "{not an array}";
  const mod = await import("./index.ts?badmodels=1");
  const iso = Bun.serve({ port: 0, fetch: mod.handle });
  // With no usable options the endpoint reverts to plain passthrough.
  const res = await fetch(`http://127.0.0.1:${iso.port}/v1/models`);
  expect((await res.json()).backend).toBe("anthropic");
  delete process.env.CLAUDE_NET_PROXY_MODELS;
  delete process.env.CLAUDE_NET_PROXY_UPSTREAM;
  iso.stop(true);
  upstream.stop(true);
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
