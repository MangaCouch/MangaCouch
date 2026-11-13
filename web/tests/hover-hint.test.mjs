import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { act, render } from "@testing-library/react";
import { HINT_DELAY_MS, HoverHints, hintText } from "../src/HoverHint.tsx";

const pointer = (type, target, pointerType = "mouse", relatedTarget = null) => {
  const event = new window.Event(type, { bubbles: true });
  Object.defineProperties(event, {
    pointerType: { value: pointerType },
    relatedTarget: { value: relatedTarget },
  });
  target.dispatchEvent(event);
};
const hint = () => document.querySelector(".hover-hint")?.textContent;

test("hint text comes from data-hint, title or aria-label, never repeating visible text", () => {
  const button = document.createElement("button");
  button.setAttribute("aria-label", "Close");
  assert.equal(hintText(button), "Close");
  button.textContent = "Close";
  assert.equal(hintText(button), "");
  button.setAttribute("title", "Close the dialog");
  assert.equal(hintText(button), "Close the dialog");
});

test("mouse hover shows one styled hint after a delay and holds back the native title", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const view = render(
    React.createElement(
      "div",
      null,
      React.createElement(HoverHints),
      React.createElement("button", { "aria-label": "Settings" }, "⚙"),
      React.createElement("span", { title: "artist:blue sky" }, "蓝天"),
    ),
  );
  const [button] = view.container.querySelectorAll("button");
  const span = view.container.querySelector("span");
  await act(async () => pointer("pointerover", button, "touch"));
  await act(async () => t.mock.timers.tick(HINT_DELAY_MS));
  assert.equal(hint(), undefined);
  await act(async () => pointer("pointerover", button));
  assert.equal(hint(), undefined);
  await act(async () => t.mock.timers.tick(HINT_DELAY_MS));
  assert.equal(hint(), "Settings");
  // Moving straight to the next element shows its hint at once.
  await act(async () => pointer("pointerout", button, "mouse", span));
  await act(async () => pointer("pointerover", span));
  assert.equal(hint(), "artist:blue sky");
  assert.equal(span.hasAttribute("title"), false);
  await act(async () => pointer("pointerout", span, "mouse", document.body));
  assert.equal(hint(), undefined);
  assert.equal(span.getAttribute("title"), "artist:blue sky");
  view.unmount();
});
test("hit areas opt out with an empty data-hint", () => {
  const scrim = document.createElement("button");
  scrim.setAttribute("aria-label", "Close reading settings");
  scrim.setAttribute("data-hint", "");
  assert.equal(hintText(scrim), "");
});
