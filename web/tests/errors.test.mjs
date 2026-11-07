import assert from "node:assert/strict";
import test from "node:test";
import { ApiError, errorMessage, request } from "../src/api.ts";

const en = (_, english) => english;
const reply = (status, body) =>
  new Response(body === undefined ? "" : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

async function failure(t, status, body) {
  const window = new EventTarget();
  const previous = globalThis.window;
  globalThis.window = window;
  t.after(() => {
    if (previous === undefined) delete globalThis.window;
    else globalThis.window = previous;
  });
  t.mock.method(globalThis, "fetch", async () => reply(status, body));
  try {
    await request("/api/anything");
  } catch (error) {
    return error;
  }
  assert.fail("request should have failed");
}

test("JSON error bodies become ApiError status, code and message", async (t) => {
  const error = await failure(t, 429, {
    error: "source_rate_limited",
    message: "Slow down",
  });
  assert.ok(error instanceof ApiError);
  assert.equal(error.status, 429);
  assert.equal(error.code, "source_rate_limited");
  assert.equal(error.detail, "Slow down");
  assert.equal(
    errorMessage(error, en),
    "The source is rate limited. Resume later.",
  );
});

test("known codes win, then the server message, then the status", async (t) => {
  assert.equal(
    errorMessage(new ApiError(403, "key_scope"), en),
    "This API key is not allowed to do that.",
  );
  assert.equal(
    errorMessage(new ApiError(422, "brand_new_code", "Title is too long"), en),
    "Title is too long",
  );
  assert.equal(
    errorMessage(new ApiError(418), en),
    "Request failed (418). Try again later.",
  );
  assert.equal(
    errorMessage(new ApiError(502, "source_unavailable"), en),
    "The source is unavailable.",
  );
});

test("400 and 422 are input problems, never a lost connection", async (t) => {
  for (const status of [400, 422]) {
    const text = errorMessage(await failure(t, status, {}), en);
    assert.doesNotMatch(text, /Connection failed/);
  }
  const unreadable = await failure(t, 422);
  assert.equal(unreadable.code, null);
  assert.match(errorMessage(unreadable, en), /did not pass validation/);
  assert.match(errorMessage(new TypeError("offline"), en), /Connection failed/);
});
