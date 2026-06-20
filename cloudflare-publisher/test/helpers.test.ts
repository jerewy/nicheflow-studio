import assert from "node:assert/strict";
import test from "node:test";

import { isRecoverableMetaError, normalizeAccountKey, parseRange, safeFileName, shouldOfferManualLocalPublish } from "../src/helpers.ts";
import { ageMinutes, statusCounts } from "../src/monitoring.ts";

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

test("summarizes active job status counts", () => {
  assert.deepEqual(
    statusCounts([
      { status: "scheduled", count: 4 },
      { status: "processing", count: 2 },
    ]),
    { scheduled: 4, processing: 2 },
  );
});

test("calculates bounded job age in minutes", () => {
  assert.equal(ageMinutes("2026-06-15T02:00:00Z", "2026-06-15T04:15:30Z"), 135);
  assert.equal(ageMinutes("2026-06-15T05:00:00Z", "2026-06-15T04:15:30Z"), 0);
  assert.equal(ageMinutes(null, "2026-06-15T04:15:30Z"), null);
});

test("treats account-level Meta blocks as recoverable", () => {
  const restricted =
    'Error: Meta HTTP 400: {"error":{"message":"API access blocked.","type":"OAuthException","code":200,"fbtrace_id":"abc"}}';
  assert.equal(isRecoverableMetaError(restricted), true);
  assert.equal(isRecoverableMetaError('Meta HTTP 400: {"error":{"code":190}}'), true); // expired token
  assert.equal(isRecoverableMetaError('Meta HTTP 400: {"error":{"code":4}}'), true); // rate limit
});

test("routes API-surface Meta failures to manual local publish option", () => {
  assert.equal(
    shouldOfferManualLocalPublish(
      'Meta HTTP 400: {"error":{"message":"API access blocked.","type":"OAuthException","code":200}}',
    ),
    true,
  );
  assert.equal(shouldOfferManualLocalPublish('Meta HTTP 400: {"error":{"code":190}}'), true);
  assert.equal(shouldOfferManualLocalPublish('Meta HTTP 400: {"error":{"code":4}}'), true);
  assert.equal(shouldOfferManualLocalPublish('Meta HTTP 400: {"error":{"code":17}}'), true);
  assert.equal(shouldOfferManualLocalPublish('Meta HTTP 400: {"error":{"code":613}}'), true);
});

test("treats genuine job failures as non-recoverable", () => {
  // code 100 = bad parameter (e.g. caption/media problem) — retrying won't help.
  assert.equal(isRecoverableMetaError('Meta HTTP 400: {"error":{"code":100,"message":"bad media"}}'), false);
  assert.equal(isRecoverableMetaError("Error: Meta did not return a container id"), false);
  assert.equal(isRecoverableMetaError("Missing Worker secret binding: IG_TOKEN_BENEATHHISTORY"), false);
});
