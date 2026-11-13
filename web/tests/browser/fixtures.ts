import { test as base, expect } from "@playwright/test";
import { spawn, type ChildProcess } from "node:child_process";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../../..", import.meta.url));

/** Starts one fixture server on a free port and resolves with its address. */
function start(): Promise<{ url: string; child: ChildProcess }> {
  const child = spawn(
    "uv",
    ["run", "python", "tests/ui_browser_server.py", "--port", "0"],
    { cwd: root, stdio: ["ignore", "pipe", "inherit"] },
  );
  return new Promise((resolve, reject) => {
    let output = "";
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error("The browser fixture server did not start in 60 s"));
    }, 60000);
    child.once("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`The browser fixture server exited with ${code}`));
    });
    child.stdout!.on("data", (chunk: Buffer) => {
      output += chunk.toString();
      const line = output.split("\n").find((item) => item.startsWith("{"));
      if (!line) return;
      clearTimeout(timer);
      child.removeAllListeners("exit");
      resolve({ url: JSON.parse(line).url, child });
    });
  });
}

function stop(child: ChildProcess): Promise<void> {
  if (child.exitCode !== null) return Promise.resolve();
  return new Promise((resolve) => {
    const force = setTimeout(() => child.kill("SIGKILL"), 5000);
    child.once("exit", () => {
      clearTimeout(force);
      resolve();
    });
    child.kill("SIGINT");
  });
}

/**
 * Each worker gets its own server and synthetic library, so specs that change the
 * library (collections, removals, downloads) can run in parallel.
 */
export const test = base.extend<object, { server: string }>({
  server: [
    async ({}, use) => {
      const { url, child } = await start();
      try {
        await use(url);
      } finally {
        await stop(child);
      }
    },
    { scope: "worker" },
  ],
  baseURL: async ({ server }, use) => use(server),
});

export { expect };
