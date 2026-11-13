// Synchronous module hooks for `module.registerHooks()`: transpile .ts/.tsx
// sources with TypeScript (Node's built-in type stripping does not handle JSX)
// and replace the Radix and sonner packages with the test shims.
import { readFileSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const shims = {
  "radix-ui": new URL("./radix.mjs", import.meta.url).href,
  sonner: new URL("./sonner.mjs", import.meta.url).href,
};

function isFile(url) {
  try {
    return statSync(fileURLToPath(url)).isFile();
  } catch {
    return false;
  }
}

export function resolve(specifier, context, nextResolve) {
  if (Object.hasOwn(shims, specifier))
    return { url: shims[specifier], shortCircuit: true };
  try {
    return nextResolve(specifier, context);
  } catch (error) {
    if (error.code !== "ERR_MODULE_NOT_FOUND" || !specifier.startsWith("."))
      throw error;
    for (const suffix of [".ts", ".tsx"]) {
      const url = new URL(specifier + suffix, context.parentURL);
      if (isFile(url)) return { url: url.href, shortCircuit: true };
    }
    throw error;
  }
}

export function load(url, context, nextLoad) {
  if (!/\.tsx?$/.test(url)) return nextLoad(url, context);
  const source = readFileSync(new URL(url), "utf8");
  return {
    format: "module",
    shortCircuit: true,
    source: ts.transpileModule(source, {
      fileName: fileURLToPath(url),
      compilerOptions: {
        module: ts.ModuleKind.ESNext,
        target: ts.ScriptTarget.ES2021,
        jsx: ts.JsxEmit.ReactJSX,
      },
    }).outputText,
  };
}
