import "./dom-environment.mjs";
import { after, afterEach } from "node:test";
import { cleanup } from "@testing-library/react";
import { registerHooks } from "node:module";
import { load, resolve } from "./typescript-loader.mjs";
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
registerHooks({ resolve, load });
const testWindow = globalThis.window;
after(() => testWindow.close());

afterEach(() => {
  cleanup();
  // Each test starts with an empty tab: the app keeps its library view in sessionStorage.
  try {
    globalThis.sessionStorage?.clear();
  } catch {
    /* A test may have replaced it with a stub. */
  }
});
