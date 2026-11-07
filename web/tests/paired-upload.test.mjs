import assert from "node:assert/strict";
import test from "node:test";
import { prepareUpload } from "../src/upload.ts";
const t = (_, en) => en;
const file = (name, size = 1) => new File([new Uint8Array(size)], name);

test("single archive stays raw; JSON filenames compare Unicode normalization", () => {
  const archive = file("e\u0301.cbz");
  assert.equal(prepareUpload([archive], null, t).body, archive);
  const upload = prepareUpload([file("é.eze.json"), archive], null, t);
  assert.ok(upload.paired && upload.body instanceof FormData);
  assert.equal(upload.archive.name, "e\u0301.cbz");
});

test("ambiguous, standalone, duplicated or unsupported selections cannot be sent", () => {
  for (const files of [
    [],
    [file("sample.json")],
    [file("a.zip"), file("b.pdf")],
    [file("a.cbz"), file("a.json"), file("a.json")],
    [file("a.cbz"), file("other.json")],
    [file("a.cbz"), file("a.txt")],
    [file("a.rar")],
  ]) {
    assert.throws(() => prepareUpload(files, null, t));
  }
});

test("archive and native/Eze byte limits are separate and exact boundaries are accepted", () => {
  assert.throws(
    () => prepareUpload([file("a.cbz", 101)], 100, t),
    /upload limit/,
  );
  for (const [name, limit] of [
    ["a.cbz.mangacouch.json", 260 * 1024],
    ["a.eze.json", 2 * 1024 ** 2],
  ]) {
    assert.equal(
      prepareUpload([file("a.cbz", 100), file(name, limit)], 100, t).paired,
      true,
    );
    assert.throws(
      () => prepareUpload([file("a.cbz", 100), file(name, limit + 1)], 100, t),
      /Metadata is too large/,
    );
  }
});
