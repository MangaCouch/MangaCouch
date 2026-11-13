import { JSDOM } from "jsdom";

const { window } = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "http://localhost/",
  pretendToBeVisual: true,
});
for (const name of [
  "window",
  "document",
  "navigator",
  "HTMLElement",
  "HTMLInputElement",
  "HTMLImageElement",
  "Element",
  "Node",
  "Event",
  "EventTarget",
  "CustomEvent",
  "PopStateEvent",
  "MouseEvent",
  "KeyboardEvent",
  "PointerEvent",
  "MutationObserver",
  "getComputedStyle",
  "localStorage",
  "location",
  "history",
]) {
  Object.defineProperty(globalThis, name, {
    configurable: true,
    writable: true,
    value: name === "window" ? window : window[name],
  });
}
Object.defineProperty(document, "hidden", {
  configurable: true,
  writable: true,
  value: false,
});
window.matchMedia = () => Object.assign(new EventTarget(), { matches: false });
HTMLElement.prototype.scrollIntoView = function () {};
HTMLElement.prototype.setPointerCapture = function () {};
HTMLElement.prototype.releasePointerCapture = function () {};
