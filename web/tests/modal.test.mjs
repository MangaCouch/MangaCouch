import { one, dispatch } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { render, act } from "@testing-library/react";
import { Modal } from "../src/Modal.tsx";
test("Modal closes once per Escape and keeps Escape inside local comboboxes", async (context) => {
  let closed = 0,
    view;
  await act(async () => {
    view = render(
      React.createElement(
        Modal,
        { title: "Dialog", closeLabel: "Close", close: () => closed++ },
        React.createElement("input", { "data-escape-local": "true" }),
      ),
    );
  });
  const dialog = one(view.container, "dialog");
  const local = one(view.container, "input");
  await act(async () => dispatch(local, "cancel"));
  assert.equal(closed, 0);
  await act(async () => dispatch(dialog, "cancel"));
  assert.equal(closed, 1);
  const keyDown = (target, prevented = false) =>
    act(async () => {
      const event = new KeyboardEvent("keydown", {
        key: "Escape",
        bubbles: true,
        cancelable: true,
      });
      if (prevented) event.preventDefault();
      target.dispatchEvent(event);
    });
  await keyDown(dialog, true);
  assert.equal(closed, 1);
  await keyDown(local);
  assert.equal(closed, 1);
  await keyDown(dialog);
  assert.equal(closed, 2);
  await act(async () => view.unmount());
});
