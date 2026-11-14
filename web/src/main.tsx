import { createRoot } from "react-dom/client";
import { App } from "./App";
import { HoverHints } from "./HoverHint";
import { ErrorBoundary } from "./ErrorBoundary";
import { forgetRetiredLibraryStorage } from "./libraryView";
import "./tokens.css";
import "./style.css";
import "./components.css";
import "./management.css";
import "./layout.css";

forgetRetiredLibraryStorage();
createRoot(document.getElementById("root")!).render(
  <ErrorBoundary
    t={(zh, en) => (navigator.language.startsWith("zh") ? zh : en)}
  >
    <App />
    <HoverHints />
  </ErrorBoundary>,
);
