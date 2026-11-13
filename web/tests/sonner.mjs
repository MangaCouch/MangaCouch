// Deterministic notification state; browser acceptance covers Sonner layout.
import React, { useEffect, useState } from "react";
let history = [],
  sequence = 0;
const listeners = new Set();
const emit = () => listeners.forEach((listener) => listener([...history]));
export function toast(title, options = {}) {
  const id = options.id ?? ++sequence;
  history = [
    ...history.filter((item) => item.id !== id),
    { id, title, ...options },
  ];
  emit();
  return id;
}
for (const type of [
  "error",
  "success",
  "info",
  "warning",
  "message",
  "loading",
])
  toast[type] = (title, options) => toast(title, { ...options, type });
toast.getHistory = () => history;
toast.dismiss = (id) => {
  history = id == null ? [] : history.filter((item) => item.id !== id);
  emit();
};
export function Toaster() {
  const [items, setItems] = useState(history);
  useEffect(() => {
    listeners.add(setItems);
    return () => listeners.delete(setItems);
  }, []);
  return React.createElement(
    "aside",
    { "aria-label": "Notifications" },
    items.map((item) =>
      React.createElement("p", { key: item.id, role: "status" }, item.title),
    ),
  );
}
