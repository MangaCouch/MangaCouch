import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./tokens.css";
import "./style.css";
import "./components.css";
import "./management.css";

createRoot(document.getElementById("root")!).render(<App />);
