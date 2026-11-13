import assert from "node:assert/strict";
import test from "node:test";
import { request, ApiError } from "../src/api.ts";

test("GET retries a short Retry-After once and preserves the server error", async (t) => {
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => {
    calls++;
    return new Response(JSON.stringify({ error: "database_busy" }), {
      status: 503,
      headers: { "Retry-After": "0" },
    });
  });
  await assert.rejects(
    request("/api/books"),
    (error) => error instanceof ApiError && error.code === "database_busy",
  );
  assert.equal(calls, 2);
});

test("writes and long Retry-After responses are never replayed", async (t) => {
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => {
    calls++;
    return new Response("{}", { status: 503, headers: { "Retry-After": "6" } });
  });
  await assert.rejects(request("/api/books"));
  await assert.rejects(request("/api/anything", { method: "POST" }));
  assert.equal(calls, 2);
});
