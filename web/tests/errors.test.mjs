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
    "The source is rate limiting; requests are halted and resume by themselves.",
  );
});

test("a source halt names its end and stays apart from sign-in throttling", async (t) => {
  const until = Math.floor(Date.now() / 1000) + 600;
  const error = await failure(t, 429, {
    error: "source_rate_limited",
    message: "Halted",
    retry_at: until,
  });
  assert.equal(error.retryAt, until);
  const text = errorMessage(error, en);
  assert.match(
    text,
    /^The source is rate limiting; requests are halted until .+\. Retry after that\.$/,
  );
  assert.doesNotMatch(text, /Too many attempts/);
  assert.equal(
    errorMessage(new ApiError(429, "rate_limited"), en),
    "Too many attempts. Try again later.",
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
  // Anything else that is not a server answer is not blamed on the connection.
  assert.equal(
    errorMessage(new DOMException("denied", "NotAllowedError"), en),
    "Something went wrong. Please retry.",
  );
});

test("a private DNS answer is diagnosable and points at the proxy settings", () => {
  const zh = (chinese) => chinese;
  const error = new ApiError(409, "source_private_address", "server text");
  assert.match(errorMessage(error, en), /private network address/);
  assert.match(errorMessage(error, en), /proxy URL/);
  assert.match(errorMessage(error, en), /fake-IP/);
  assert.match(errorMessage(error, zh), /内网地址/);
  assert.match(errorMessage(error, zh), /代理地址/);
  assert.doesNotMatch(
    errorMessage(new ApiError(409, "unsafe_source_url"), en),
    /proxy/,
  );
});

for (const [code, reason] of [
  ["login_rate_limited", "Too many sign-in attempts."],
  ["login_busy", "Sign-in verification is busy."],
]) {
  test(`login retry explains ${code} and Retry-After`, async (t) => {
    const { failure: decodeFailure } = await import("../src/api.ts");
    t.mock.method(Date, "now", () => 1000000);
    const error = await decodeFailure(
      new Response(JSON.stringify({ error: code }), {
        status: 429,
        headers: { "Content-Type": "application/json", "Retry-After": "17" },
      }),
    );
    assert.equal(errorMessage(error, en), `${reason} Try again in 17 seconds.`);
    assert.match(
      errorMessage(error, (zh) => zh),
      /17 秒后重试/,
    );
  });
}
