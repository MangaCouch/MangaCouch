import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { act, render } from "@testing-library/react";
import { fireEvent } from "@testing-library/dom";
import { RouteLink } from "../src/navigation.tsx";

test("navigation links preserve native modified clicks and intercept only an ordinary click", async () => {
  const routes = [];
  const view = render(
    React.createElement(
      RouteLink,
      { route: "book/one", navigate: (route) => routes.push(route) },
      "Book",
    ),
  );
  const link = view.getByRole("link", { name: "Book" });
  assert.equal(link.getAttribute("href"), "#book/one");
  for (const keys of [
    { ctrlKey: true },
    { metaKey: true },
    { shiftKey: true },
    { button: 1 },
  ]) {
    let native;
    link.addEventListener(
      "click",
      (event) => {
        native = event;
      },
      { once: true },
    );
    await act(async () => fireEvent.click(link, keys));
    assert.equal(native.defaultPrevented, false);
  }
  assert.deepEqual(routes, []);
  await act(async () => fireEvent.click(link));
  assert.deepEqual(routes, ["book/one"]);
});
