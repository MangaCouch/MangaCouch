// Every stable error code the backend can send has localized text in the web UI.
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { ApiError, codeMessage, errorMessage } from "../src/api.ts";
import { sourceErrors, sourceMessage } from "../src/sourceErrors.ts";

const backend = fileURLToPath(
  new URL("../../src/mangacouch/", import.meta.url),
);
const zh = (chinese) => chinese;
const en = (_, english) => english;
const CODE = String.raw`["']([a-z][a-z0-9_]*)["']`;

function pythonFiles(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory())
      return entry.name === "__pycache__" ? [] : pythonFiles(path);
    return entry.name.endsWith(".py") ? [path] : [];
  });
}

/**
 * The codes the backend raises, by where they come from: `code="…"` on the domain
 * exceptions (and `code = "…"` on their classes), `error_response(status, "…")` and the
 * middleware's `reject(status, "…")`, `"error": "…"` result bodies, sidecar sync failures
 * (`_error(book_id, "…")`, plus `archive_<availability>`), the HTTP status codes
 * (`HTTP_CODES`) and `SourceError("…")`, which also ends up on download and source-check
 * records shown through `sourceMessage`.
 */
function backendCodes() {
  const api = new Map(),
    source = new Map();
  const add = (map, code, file) => {
    if (!map.has(code)) map.set(code, new Set());
    map.get(code).add(file.slice(backend.length));
  };
  const patterns = [
    new RegExp(String.raw`\b(?:code|reason)\s*=\s*${CODE}`, "g"),
    new RegExp(
      String.raw`\b(?:error_response|reject)\(\s*\d{3}\s*,\s*${CODE}`,
      "g",
    ),
    new RegExp(String.raw`["']error["']\s*:\s*${CODE}`, "g"),
    // Sidecar sync failures recorded per book (`self._error(book_id, "…")`).
    new RegExp(String.raw`\b_error\(\s*[\w.]+\s*,\s*${CODE}`, "g"),
  ];
  for (const file of pythonFiles(backend)) {
    const text = readFileSync(file, "utf8");
    for (const pattern of patterns)
      for (const [, code] of text.matchAll(pattern)) add(api, code, file);
    // Codes written straight onto a download (`self._update(…, error="…")`).
    for (const [, code] of text.matchAll(
      new RegExp(String.raw`\b_update\([^)]*?\berror=${CODE}`, "g"),
    )) {
      add(api, code, file);
      add(source, code, file);
    }
    for (const [, code] of text.matchAll(
      new RegExp(String.raw`\bSourceError\(\s*${CODE}`, "g"),
    )) {
      add(api, code, file);
      add(source, code, file);
    }
    const errorReason = text.match(
      /def error_reason\([^]*?(?=\n(?:def |class |[^ \n])|$)/,
    );
    if (errorReason)
      for (const [, code] of errorReason[0].matchAll(
        new RegExp(String.raw`return\s+${CODE}`, "g"),
      ))
        add(api, code, file);
    const statuses = text.match(/^HTTP_CODES\s*=\s*\{([\s\S]*?)^\}/m);
    if (statuses)
      for (const [, code] of statuses[1].matchAll(
        new RegExp(String.raw`\d{3}\s*:\s*${CODE}`, "g"),
      ))
        add(api, code, file);
  }
  // `_error(book_id, f"archive_{state}")` for a book whose file is not available.
  for (const state of ["missing", "changed", "unreadable"])
    add(api, `archive_${state}`, join(backend, "sidecars.py"));
  return { api, source };
}
const listed = (codes) =>
  codes.map((code) => `  ${code[0]} (${[...code[1]].join(", ")})`).join("\n");

test("the backend code extraction finds the codes it is meant to", () => {
  const { api, source } = backendCodes();
  for (const code of [
    "same_origin_required",
    "cross_site_request",
    "invalid_host",
    "book_not_found",
    "revision_conflict",
    "invalid_archive",
    "request_timeout",
    "unsupported_source",
    "sidecar_invalid",
  ])
    assert.ok(api.has(code), `extraction missed ${code}`);
  for (const code of ["source_network", "download_local_error"])
    assert.ok(source.has(code), `extraction missed SourceError ${code}`);
  assert.ok(api.size > 50, `only ${api.size} codes found`);
});

test("every backend error code has Chinese and English text", () => {
  const { api } = backendCodes();
  const missing = [...api].filter(
    ([code]) =>
      codeMessage(code, zh) === null || codeMessage(code, en) === null,
  );
  assert.equal(
    missing.length,
    0,
    `Add these codes to api.ts or sourceErrors.ts:\n${listed(missing)}`,
  );
  // A Chinese interface never falls back to the server's English message for them.
  for (const [code] of api) {
    const text = errorMessage(new ApiError(400, code, "English detail"), zh);
    assert.notEqual(text, "English detail", code);
    assert.match(text, /[一-鿿]/, `${code} has no Chinese text`);
    assert.notEqual(codeMessage(code, zh), codeMessage(code, en), code);
  }
});

test("codes stored on downloads and source checks have their own source text", () => {
  const { source } = backendCodes();
  // Disk-full and missing-library failures end downloads with these codes too.
  for (const code of ["insufficient_storage", "root_unavailable"])
    if (!source.has(code)) source.set(code, new Set(["downloads"]));
  const missing = [...source].filter(([code]) => !sourceErrors[code]);
  assert.equal(
    missing.length,
    0,
    `Add these codes to sourceErrors.ts:\n${listed(missing)}`,
  );
  assert.equal(
    sourceMessage("insufficient_storage", en),
    "Downloads are waiting for disk space; checked every 5 minutes and resumed automatically.",
  );
});

test("an origin mismatch names the setting to check", () => {
  for (const code of ["same_origin_required", "cross_site_request"]) {
    const error = new ApiError(403, code, "Requests must come from this site");
    assert.match(errorMessage(error, zh), /MANGACOUCH_ORIGIN/);
    assert.match(errorMessage(error, en), /MANGACOUCH_ORIGIN/);
    assert.match(errorMessage(error, zh), /地址/);
  }
});
