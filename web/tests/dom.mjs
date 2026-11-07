import { fireEvent } from "@testing-library/react";

export function all(container, selector) {
  if (typeof selector === "string")
    return [...container.querySelectorAll(selector)];
  return [...container.querySelectorAll("*")].filter((node) =>
    Object.entries(selector).every(
      ([name, value]) => attr(node, name) === value,
    ),
  );
}

export function one(container, selector) {
  const nodes = all(container, selector);
  if (nodes.length !== 1)
    throw new Error(
      `Expected one ${JSON.stringify(selector)}, found ${nodes.length}`,
    );
  return nodes[0];
}

export function attr(node, name) {
  if (name === "value")
    return ["range", "number"].includes(node.type)
      ? Number(node.value)
      : node.value;
  if (["checked", "disabled"].includes(name)) return node[name];
  if (name === "style")
    return node.hasAttribute("style") ? node.style : undefined;
  if (name === "className") return node.getAttribute("class") ?? undefined;
  if (["inert", "download", "multiple"].includes(name))
    return node.hasAttribute(name) || undefined;
  const value = node.getAttribute(name);
  if (name.startsWith("aria-") && ["true", "false"].includes(value))
    return value === "true";
  return value ?? undefined;
}

/** Dispatch real DOM events, including properties that jsdom does not lay out. */
export function dispatch(node, type, init = {}) {
  if (!node) throw new Error(`Cannot dispatch ${type}: element is missing`);
  const {
    currentTarget,
    preventDefault,
    stopPropagation: _stop,
    nativeEvent,
    ...options
  } = init;
  for (const [name, value] of Object.entries(currentTarget || {})) {
    Object.defineProperty(node, name, {
      configurable: true,
      writable: true,
      value,
    });
  }
  if (type === "change" && ["checkbox", "radio"].includes(node.type)) {
    if (node.checked === options.target?.checked) return;
    return fireEvent.click(node);
  }
  if (type === "clickCapture") type = "click";
  if (type === "pointerEnter") type = "pointerOver";
  const result = fireEvent[type]
    ? fireEvent[type](node, { ...nativeEvent, ...options })
    : fireEvent(node, new Event(type, { bubbles: true, cancelable: true }));
  if (!result) preventDefault?.();
  return result;
}
