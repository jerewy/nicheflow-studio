import assert from "node:assert/strict";
import test from "node:test";

import { normalizeAccountKey, parseRange, safeFileName } from "../src/helpers.ts";

test("normalizes valid account keys", () => {
  assert.equal(normalizeAccountKey(" PastMomentsDaily "), "pastmomentsdaily");
});

test("rejects unsafe account keys", () => {
  assert.throws(() => normalizeAccountKey("../secret"));
});

test("sanitizes file names", () => {
  assert.equal(safeFileName("my reel (final).mp4"), "my_reel_final_.mp4");
});

test("parses full and suffix byte ranges", () => {
  assert.deepEqual(parseRange("bytes=10-19", 100), { offset: 10, length: 10 });
  assert.deepEqual(parseRange("bytes=-25", 100), { suffix: 25 });
  assert.deepEqual(parseRange("bytes=90-", 100), { offset: 90, length: 10 });
});
