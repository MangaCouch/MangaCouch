import "./dom-environment.mjs";
import { afterEach } from "node:test";
import { cleanup } from "@testing-library/react";
import { registerHooks } from "node:module";
import { load, resolve } from "./typescript-loader.mjs";
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
registerHooks({ resolve, load });

afterEach(() => cleanup());
