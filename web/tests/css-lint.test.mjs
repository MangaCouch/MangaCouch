import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { test } from "node:test";

const src = new URL("../src/", import.meta.url);
const files = readdirSync(src);
const css = Object.fromEntries(
  files
    .filter((name) => name.endsWith(".css"))
    .map((name) => [name, readFileSync(new URL(name, src), "utf8")]),
);
const code = files
  .filter((name) => /\.tsx?$/.test(name))
  .map((name) => readFileSync(new URL(name, src), "utf8"))
  .join("\n");
// Classes built from a template prefix such as `pill-${kind}` or `fit-${fit}`.
const prefixes = [...code.matchAll(/([a-z][a-z0-9-]*-)\$\{/g)].map(
  (match) => match[1],
);
const external = /^lucide-/;

function selectors(text) {
  const withoutComments = text.replace(/\/\*[\s\S]*?\*\//g, "");
  return [...withoutComments.matchAll(/([^{}@;]+)\{/g)]
    .map((match) => match[1].trim())
    .filter((selector) => !/^(from|to|\d+%)$/.test(selector));
}

test("every class selector in the stylesheets is used by a component", () => {
  const unused = new Set();
  for (const [name, text] of Object.entries(css))
    for (const selector of selectors(text))
      for (const [, cls] of selector
        .replace(/\[[^\]]*\]/g, "")
        .matchAll(/\.([a-zA-Z_][\w-]*)/g)) {
        if (external.test(cls) || prefixes.some((p) => cls.startsWith(p)))
          continue;
        const used = new RegExp(`(^|[^\\w-])${cls}($|[^\\w-])`).test(code);
        if (!used) unused.add(`${name}: .${cls}`);
      }
  assert.deepEqual([...unused], []);
});

test("every custom property read in the stylesheets is defined", () => {
  const all = Object.values(css).join("\n");
  const defined = new Set(
    [...all.matchAll(/(--[\w-]+)\s*:/g)].map((match) => match[1]),
  );
  // Set at runtime by components (style={{ "--name": ... }}) or by Radix.
  const runtime = (name) =>
    name.startsWith("--radix-") || code.includes(`"${name}"`);
  const missing = [...all.matchAll(/var\(\s*(--[\w-]+)/g)]
    .map((match) => match[1])
    .filter((name) => !defined.has(name) && !runtime(name));
  assert.deepEqual([...new Set(missing)], []);
});

test("every design token is used", () => {
  const tokens = css["tokens.css"];
  const rest = Object.entries(css)
    .filter(([name]) => name !== "tokens.css")
    .map(([, text]) => text)
    .join("\n");
  const used = (name) =>
    rest.includes(`var(${name})`) ||
    tokens.includes(`var(${name})`) ||
    code.includes(name);
  const unused = [
    ...new Set(
      [...tokens.matchAll(/^\s*(--[\w-]+)\s*:/gm)].map((match) => match[1]),
    ),
  ].filter((name) => !used(name));
  assert.deepEqual(unused, []);
});

test("the prefixed backdrop-filter comes first, so the minifier keeps the standard one", () => {
  for (const [name, text] of Object.entries(css))
    assert.doesNotMatch(
      text,
      /(?<!-webkit-)backdrop-filter:[^;]*;\s*-webkit-backdrop-filter:/,
      name,
    );
});
