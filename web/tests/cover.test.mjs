import { one, attr, dispatch } from "./dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { render, act } from "@testing-library/react";
import { Cover } from "../src/Cover.tsx";
async function setup(t, id, sha256) {
  const requests = [];
  t.mock.method(globalThis, "fetch", async (...args) => {
    requests.push(args);
    throw Error("Unexpected full page fetch");
  });
  let view;
  await act(async () => {
    view = render(
      React.createElement(Cover, { id, sha256, alt: "Book cover" }),
    );
  });
  t.after(async () => {
    await act(async () => view.unmount());
  });
  return { view, requests };
}
test("book covers use authenticated native lazy thumbnails without fetching full pages into JS", async (t) => {
  const x = await setup(t, "book-id");
  const img = one(x.view.container, "img");
  assert.equal(attr(img, "src"), "/api/books/book-id/cover");
  assert.equal(attr(img, "loading"), "lazy");
  assert.equal(attr(img, "decoding"), "async");
  assert.equal(attr(img, "alt"), "Book cover");
  assert.equal(attr(img, "crossOrigin"), undefined);
  assert.deepEqual(x.requests, []);
});
test("covers carry the content version so the server can cache them as immutable", async (t) => {
  const x = await setup(t, "book-id", "0123456789abcdef0123456789abcdef");
  assert.equal(
    attr(one(x.view.container, "img"), "src"),
    "/api/books/book-id/cover?v=0123456789abcdef",
  );
});
test("a failed cover leaves a placeholder, a successful retry is visible and changing book remounts the image", async (t) => {
  const x = await setup(t, "old-id");
  const image = one(x.view.container, "img");
  await act(async () => dispatch(image, "error"));
  assert.equal(image.style.visibility, "hidden");
  await act(async () => dispatch(image, "load"));
  assert.equal(image.style.visibility, "visible");
  await act(async () =>
    x.view.rerender(React.createElement(Cover, { id: "new-id" })),
  );
  const fresh = one(x.view.container, "img");
  assert.notEqual(fresh, image);
  assert.equal(attr(fresh, "src"), "/api/books/new-id/cover");
  assert.equal(attr(fresh, "style"), undefined);
  assert.deepEqual(x.requests, []);
});
