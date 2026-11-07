import { all, one, dispatch, attr } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { render, act } from "@testing-library/react";
import { Credentials } from "../src/Credentials.tsx";
import { PasscodeChange } from "../src/Passcode.tsx";
import { App } from "../src/App.tsx";
import { storageKey } from "../src/device.ts";
import { button, click, setupFixture, visibleText } from "./harness.mjs";
async function setup(t, prepare = () => {}, as = "keys") {
  const window = globalThis.window,
    document = globalThis.document;
  document.hidden = false;
  let hash = "#settings";
  const location = {
    pathname: "/",
    search: "",
    get hash() {
      return hash;
    },
    set hash(value) {
      hash = value.startsWith("#") ? value : `#${value}`;
      queueMicrotask(() => window.dispatchEvent(new Event("hashchange")));
    },
  };
  const stored = new Map([[storageKey("language"), "en"]]),
    writes = [],
    clipboard = [];
  const x = await setupFixture(t, prepare, {
    window,
    globals: {
      document,
      location,
      history: {
        replaceState: (_, __, url) => {
          hash = url.includes("#") ? url.slice(url.indexOf("#")) : "";
        },
      },
      localStorage: {
        getItem: (key) => stored.get(key) ?? null,
        setItem: (key, value) => {
          stored.set(key, value);
          writes.push([key, value]);
        },
      },
      navigator: {
        clipboard: { writeText: async (value) => clipboard.push(value) },
      },
    },
  });
  const guard = { current: null },
    locks = [];
  let lockedEvents = 0;
  window.addEventListener("mc-locked", () => lockedEvents++);
  const view = await x.render(
    as === "app"
      ? React.createElement(App)
      : as === "passcode"
        ? React.createElement(PasscodeChange, {
            t: (_, en) => en,
            lock: (confirmed) => locks.push(confirmed),
          })
        : React.createElement(Credentials, { t: (_, en) => en, guard }),
  );
  const control = (text, kind = "input") =>
    one(
      all(view.container, "label").find((node) =>
        node.textContent.includes(text),
      ),
      kind,
    );
  return {
    fixture: x.fixture,
    view,
    guard,
    locks,
    clipboard,
    writes,
    window,
    unmount: () => x.unmount(view),
    button: (text) => button(view, text),
    control,
    events: () => lockedEvents,
    focus: () => act(async () => window.dispatchEvent(new Event("focus"))),
    /** Expands a collapsed settings section by its title (App only). */
    open: async (title) => {
      const details = all(view.container, "details").find((node) =>
        one(node, "h3").textContent.includes(title),
      );
      await act(async () =>
        dispatch(details, "toggle", { currentTarget: { open: true } }),
      );
    },
    text: () => visibleText(view),
    click: (text) => click(button(view, text)),
    input: async (text, value) => {
      await act(async () =>
        dispatch(control(text), "change", {
          target: { value, checked: value },
        }),
      );
    },
    submit: async (index = 0) => {
      await act(async () =>
        dispatch(all(view.container, "form")[index], "submit", {
          preventDefault() {},
        }),
      );
    },
  };
}
async function password(x, value = "new-fixture-passcode") {
  await x.input("New passcode (8 to 256 characters)", value);
  await x.input("Repeat new passcode", value);
  await x.input(
    "I understand all web devices will sign out and require the new passcode.",
    true,
  );
}
test("credentials page lists API keys only and defaults to read-only without mutations", async (t) => {
  const x = await setup(t);
  assert.ok(x.fixture.requests.every((r) => r.method === "GET"));
  assert.match(x.text(), /Automation/);
  assert.doesNotMatch(x.text(), /Living room tablet/);
  assert.equal(attr(x.control("Permission", "select"), "value"), "readonly");
  assert.equal(attr(x.button("Create key"), "disabled"), true);
  await x.input("Name", " ");
  assert.equal(attr(x.button("Create key"), "disabled"), true);
  assert.equal(await x.guard.current(), true);
});
test("a downloads key can be created for the browser extension", async (t) => {
  const x = await setup(t);
  const options = all(x.control("Permission", "select"), "option").map((node) =>
    attr(node, "value"),
  );
  assert.deepEqual(options, ["readonly", "downloads", "management"]);
  await act(async () =>
    dispatch(x.control("Permission", "select"), "change", {
      target: { value: "downloads" },
    }),
  );
  assert.match(x.text(), /can only submit download links/);
  await x.input("Name", "Browser extension");
  await x.submit();
  assert.equal(x.fixture.credentials.at(-1).kind, "downloads");
  assert.match(attr(one(x.view.container, "textarea"), "value"), /^mcd_/);
  assert.match(x.text(), /Browser extension: can only add downloads/);
});
test("created key is revealed once, copied only on request and never persisted to browser storage", async (t) => {
  const x = await setup(t);
  await x.input("Name", " Tablet reader ");
  await x.submit();
  const key = attr(one(x.view.container, "textarea"), "value");
  assert.match(key, /^mcr_fixture-only/);
  assert.equal(x.fixture.credentials.at(-1).label, "Tablet reader");
  assert.equal(x.fixture.credentials.at(-1).kind, "readonly");
  assert.equal(x.fixture.keySequence, 1);
  assert.equal(x.clipboard.length, 0);
  assert.equal(await x.guard.current(), false);
  assert.equal(attr(x.button("Create key"), "disabled"), true);
  await x.click("Copy key");
  assert.deepEqual(x.clipboard, [key]);
  assert.ok(x.button("Copied"));
  await x.click("Saved; close display");
  assert.equal(await x.guard.current(), true);
  await x.focus();
  assert.ok(!x.text().includes(key));
  assert.ok(!JSON.stringify(x.writes).includes(key));
  assert.ok(!JSON.stringify(x.fixture.credentials).includes(key));
});
test("management permission is explicit and clipboard rejection preserves the manual-copy field", async (t) => {
  const x = await setup(t);
  await act(async () =>
    dispatch(x.control("Permission", "select"), "change", {
      target: { value: "management" },
    }),
  );
  assert.match(x.text(), /creating or revoking other keys/);
  await x.input("Name", "Automation 2");
  await x.submit();
  t.mock.method(navigator.clipboard, "writeText", async () => {
    throw Error("blocked");
  });
  await x.click("Copy key");
  assert.match(x.text(), /Copy failed/);
  assert.match(
    attr(one(x.view.container, "textarea"), "value"),
    /^mcm_fixture-only/,
  );
  assert.equal(x.fixture.credentials.at(-1).kind, "management");
});
test("a failed creation shows the error and reloads the list so a stray key can be revoked", async (t) => {
  const x = await setup(t, (f) => (f.keyCreateLoseResponse = true));
  await x.input("Name", "Lost key");
  await x.submit();
  assert.equal(x.fixture.keySequence, 1);
  assert.match(x.text(), /Connection failed/);
  assert.equal(all(x.view.container, "textarea").length, 0);
  assert.ok(x.button("Revoke Lost key"));
  await x.click("Revoke Lost key");
  await x.click("Confirm revocation");
  assert.ok(x.fixture.credentials.at(-1).revoked_at);
  assert.match(x.text(), /Key revoked/);
});
test("revocation requires explicit confirmation and a lost response is reconciled by the reload", async (t) => {
  const x = await setup(t, (f) => (f.keyRevokeLoseResponse = true));
  await x.click("Revoke Automation");
  await x.click("Cancel");
  assert.equal(
    x.fixture.requests.filter((r) => r.method === "DELETE").length,
    0,
  );
  await x.click("Revoke Automation");
  await x.click("Confirm revocation");
  assert.match(x.text(), /Connection failed/);
  assert.equal(x.button("Revoke Automation"), undefined);
  await x.input("Show revoked", true);
  assert.match(x.text(), /AutomationFull access Revoked/);
});
test("a failed background refresh clears its error after the next successful one", async (t) => {
  const x = await setup(t);
  x.fixture.credentialListFailure = true;
  await x.focus();
  assert.match(x.text(), /Library storage is unavailable/);
  x.fixture.credentialListFailure = false;
  await x.focus();
  assert.doesNotMatch(x.text(), /Library storage is unavailable/);
  assert.match(x.text(), /Automation/);
});
test("a duplicate submit is ignored and a response after unmount reveals nothing", async (t) => {
  const x = await setup(t);
  await x.input("Name", "In flight");
  const original = globalThis.fetch;
  let release, first;
  t.mock.method(globalThis, "fetch", async (path, options) => {
    const response = await original(path, options);
    if (path === "/api/auth/keys")
      await new Promise((resolve) => (release = resolve));
    return response;
  });
  await act(async () => {
    const submit = () => dispatch(all(x.view.container, "form")[0], "submit");
    first = submit({ preventDefault() {} });
    await submit({ preventDefault() {} });
  });
  assert.equal(x.fixture.keySequence, 1);
  await x.unmount();
  release();
  await act(async () => first);
  assert.equal(x.clipboard.length, 0);
  assert.equal(x.guard.current, null);
});
test("a signed-in manager changes the passcode without entering the old passcode", async (t) => {
  const x = await setup(t, () => {}, "passcode");
  await password(x);
  assert.equal(
    all(x.view.container, "input").filter(
      (node) => attr(node, "autoComplete") === "current-password",
    ).length,
    0,
  );
  await x.submit();
  assert.deepEqual(x.locks, [true]);
  assert.equal(
    attr(x.control("New passcode (8 to 256 characters)"), "value"),
    "",
  );
  assert.ok(
    x.fixture.credentials
      .filter((c) => c.kind === "session")
      .every((c) => c.revoked_at),
  );
  assert.equal(
    x.fixture.credentials.find((c) => c.kind === "management").revoked_at,
    null,
  );
});
test("passcode validation requires matching values, acknowledgement and codepoint length", async (t) => {
  const x = await setup(t, () => {}, "passcode");
  const submitLabel = "Change passcode and sign out web devices";
  await password(x, "short");
  assert.equal(attr(x.button(submitLabel), "disabled"), true);
  await password(x, "😀".repeat(256));
  assert.equal(attr(x.button(submitLabel), "disabled"), false);
  await x.input("Repeat new passcode", "different");
  assert.equal(attr(x.button(submitLabel), "disabled"), true);
  assert.match(x.text(), /New passcodes do not match/);
  await password(x, "😀".repeat(257));
  assert.equal(attr(x.button(submitLabel), "disabled"), true);
  assert.equal(x.fixture.passcodeChanges, 0);
});
test("a failed passcode change keeps the form and reports the error without locking", async (t) => {
  const x = await setup(t, (f) => (f.passcodeFailure = 429), "passcode");
  await password(x);
  await x.submit();
  assert.match(x.text(), /Too many attempts/);
  assert.deepEqual(x.locks, []);
  assert.equal(x.events(), 0);
  x.fixture.passcodeFailure = 401;
  await x.submit();
  assert.equal(x.events(), 1);
});
test("App clears the revealed key when authentication locks and login supports long Unicode input", async (t) => {
  const x = await setup(t, () => {}, "app");
  await x.open("API keys");
  await x.input("Name", "App key");
  await x.submit();
  const key = attr(one(x.view.container, "textarea"), "value");
  await act(async () => x.window.dispatchEvent(new Event("mc-locked")));
  assert.ok(!x.text().includes(key));
  assert.ok(x.button("Open library"));
  assert.equal(one(x.view.container, "input").maxLength, 512);
  assert.ok(!JSON.stringify(x.writes).includes(key));
});
test("App passcode change enters login with completion notice and clears password fields", async (t) => {
  const x = await setup(t, () => {}, "app");
  await x.open("Change library passcode");
  await password(x);
  await act(async () =>
    dispatch(
      one(x.view.container, { className: "passcode-change" }),
      "submit",
      { preventDefault() {} },
    ),
  );
  assert.ok(x.button("Open library"));
  assert.match(x.text(), /Passcode updated. Sign in again/);
  assert.equal(attr(one(x.view.container, "input"), "value"), "");
  assert.ok(!x.text().includes("new-fixture-passcode"));
});
