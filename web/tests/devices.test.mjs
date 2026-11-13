import { all, attr, dispatch } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import { act } from "@testing-library/react";
import {
  Devices,
  deviceKind,
  deviceLabel,
  readableDevice,
} from "../src/Devices.tsx";
import { english, installGlobals, renderPage } from "./harness.mjs";

const chinese = (zh) => zh;

test("device kinds follow the sign-in label, user-agent shaped or hand-made", () => {
  assert.equal(deviceKind("iPhone / Safari"), "phone");
  assert.equal(deviceKind("Android / Chrome"), "phone");
  assert.equal(deviceKind("Android tablet / Chrome"), "tablet");
  assert.equal(deviceKind("iPad / Safari"), "tablet");
  assert.equal(deviceKind("Living room tablet"), "tablet");
  assert.equal(deviceKind("Mac / Safari"), "computer");
  assert.equal(deviceKind("Windows / Edge"), "computer");
  assert.equal(deviceKind("Linux / Firefox"), "computer");
  assert.equal(deviceKind("Web browser"), "unknown");
  assert.equal(deviceKind("Browser / Web"), "unknown");
});

test("device labels read as browser on system", () => {
  assert.equal(readableDevice("iPhone / Safari", english), "Safari on iPhone");
  assert.equal(
    readableDevice("Android tablet / Chrome", chinese),
    "安卓平板 上的 Chrome",
  );
  assert.equal(
    readableDevice("Browser / Web", english),
    "a browser on unknown system",
  );
  assert.equal(
    readableDevice("Living room tablet", english),
    "Living room tablet",
  );
});

test("an Android user agent without Mobile is labelled a tablet", () => {
  const agent = (userAgent) => {
    const restore = installGlobals({
      navigator: { userAgent, maxTouchPoints: 5 },
    });
    try {
      return deviceLabel();
    } finally {
      restore();
    }
  };
  assert.equal(
    agent(
      "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36",
    ),
    "Android / Chrome",
  );
  assert.equal(
    agent(
      "Mozilla/5.0 (Linux; Android 14; SM-X710) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    ),
    "Android tablet / Chrome",
  );
});

async function devices(t) {
  const logouts = [];
  const x = await renderPage(t, Devices, {
    prepare: (fixture) => {
      fixture.credentials[0].label = "iPhone / Safari";
      fixture.credentials.push({
        id: "fixture-mac",
        kind: "session",
        label: "Mac / Chrome",
        created_at: Math.floor(Date.now() / 1000) - 600,
        expires_at: Math.floor(Date.now() / 1000) + 86400,
        revoked_at: null,
      });
    },
    props: { logout: () => logouts.push(true) },
  });
  return { ...x, logouts };
}

test("each web device shows an icon and name for its kind", async (t) => {
  const x = await devices(t);
  const rows = all(x.view.container, ".device-list > li");
  assert.deepEqual(
    rows.map((row) => attr(row, "data-device-kind")),
    ["phone", "tablet", "computer"],
  );
  assert.ok(rows.every((row) => row.querySelector("svg")));
  assert.match(rows[0].textContent, /Safari on iPhone/);
  assert.match(rows[0].textContent, /This device/);
  assert.match(rows[0].textContent, /Phone · Signed in/);
  assert.match(rows[1].textContent, /Living room tablet/);
  assert.match(rows[1].textContent, /Tablet · Signed in/);
  assert.match(rows[2].textContent, /Chrome on Mac/);
  assert.match(rows[2].textContent, /Computer · Signed in/);
});

test("signing out this device asks first and can be cancelled", async (t) => {
  const x = await devices(t);
  const signOut = () =>
    all(x.view.container, ".device-list > li")[0].querySelector("button");
  await act(async () => dispatch(signOut(), "click"));
  assert.deepEqual(x.logouts, []);
  assert.match(x.text(), /Sign out this device\?/);
  assert.match(x.text(), /You are using this device/);
  await act(async () => x.click("Cancel"));
  assert.doesNotMatch(x.text(), /Sign out this device\?/);
  assert.deepEqual(x.logouts, []);
  await act(async () => dispatch(signOut(), "click"));
  const confirm = all(x.view.container, "button").filter(
    (node) =>
      node.textContent === "Sign out" && attr(node, "class") === "danger",
  );
  assert.equal(confirm.length, 1);
  await act(async () => dispatch(confirm[0], "click"));
  assert.deepEqual(x.logouts, [true]);
  // This device signs out through the app's sign-out, not by revoking its own row.
  assert.equal(x.fixture.credentials[0].revoked_at, null);
});
