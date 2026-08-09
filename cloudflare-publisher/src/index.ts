interface Env {
  DB: D1Database;
  MEDIA: R2Bucket;
  API_KEY: string;
  PUBLISH_MODE: "disabled" | "validate" | "live";
  GRAPH_HOST: string;
  GRAPH_VERSION: string;
  PUBLIC_BASE_URL: string;
  MAX_STORED_BYTES: string;
  MAX_UPLOAD_BYTES: string;
  MAX_ACTIVE_JOBS: string;
  [secretName: string]: unknown;
}

interface AccountRow {
  account_key: string;
  instagram_user_id: string;
  token_secret_name: string;
  enabled: number;
  daily_limit: number;
  min_gap_minutes: number;
}

interface JobRow {
  id: string;
  external_id: string;
  account_key: string;
  caption: string;
  scheduled_at: string;
  status: string;
  media_key: string;
  media_token: string;
  content_type: string;
  meta_container_id: string | null;
  processing_started_at: string | null;
  attempts: number;
}

interface AccountInput {
  account_key: string;
  instagram_user_id: string;
  token_secret_name: string;
  enabled?: boolean;
  daily_limit?: number;
  min_gap_minutes?: number;
}

interface JobInput {
  external_id: string;
  account_key: string;
  caption?: string;
  scheduled_at: string;
  file_name?: string;
  content_type?: string;
}

import { isRecoverableMetaError, normalizeAccountKey, parseRange, safeFileName, shouldOfferManualLocalPublish } from "./helpers";
import {
  ageMinutes,
  STALE_AWAITING_UPLOAD_MINUTES,
  STALE_PROCESSING_MINUTES,
  statusCounts,
  type StatusCountRow,
} from "./monitoring";

const JSON_HEADERS = { "content-type": "application/json; charset=utf-8" };
const ACTIVE_MEDIA_STATUSES = ["scheduled", "processing"];
const STORED_MEDIA_STATUSES = ["scheduled", "processing", "awaiting_upload"];
const MAX_JOBS_PER_TICK = 5;
const MAX_ATTEMPTS = 3;
// When Meta blocks the whole account (restriction / token / rate limit) the job is not at
// fault, so we back off this long and retry without burning attempts or deleting media...
const BLOCK_RETRY_MINUTES = 30;
// ...but stop eventually so a permanent block can't pin media in storage forever.
const BLOCK_GIVEUP_HOURS = 48;

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value, null, 2), { status, headers: JSON_HEADERS });
}

function errorResponse(error: unknown, status = 400): Response {
  const message = error instanceof Error ? error.message : String(error);
  return json({ error: message }, status);
}

function requireAuth(request: Request, env: Env): void {
  const expected = `Bearer ${env.API_KEY}`;
  if (!env.API_KEY || request.headers.get("authorization") !== expected) {
    throw new Response("Unauthorized", { status: 401 });
  }
}

async function readJson<T>(request: Request): Promise<T> {
  if (!request.headers.get("content-type")?.includes("application/json")) {
    throw new Error("Expected application/json");
  }
  return request.json<T>();
}

function nowIso(): string {
  return new Date().toISOString();
}

function addMinutes(iso: string, minutes: number): string {
  return new Date(Date.parse(iso) + minutes * 60_000).toISOString();
}

function parseScheduledAt(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) {
    throw new Error("scheduled_at must be a valid ISO-8601 timestamp");
  }
  return parsed.toISOString();
}

async function upsertAccount(request: Request, env: Env): Promise<Response> {
  const input = await readJson<AccountInput>(request);
  const accountKey = normalizeAccountKey(input.account_key);
  if (!input.instagram_user_id?.trim() || !input.token_secret_name?.trim()) {
    throw new Error("instagram_user_id and token_secret_name are required");
  }
  const dailyLimit = input.daily_limit ?? 6;
  const minGapMinutes = input.min_gap_minutes ?? 240;
  if (dailyLimit < 1 || dailyLimit > 20 || minGapMinutes < 30) {
    throw new Error("daily_limit must be 1-20 and min_gap_minutes must be at least 30");
  }
  const now = nowIso();
  await env.DB.prepare(
    `INSERT INTO accounts (
       account_key, instagram_user_id, token_secret_name, enabled,
       daily_limit, min_gap_minutes, created_at, updated_at
     ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT(account_key) DO UPDATE SET
       instagram_user_id = excluded.instagram_user_id,
       token_secret_name = excluded.token_secret_name,
       enabled = excluded.enabled,
       daily_limit = excluded.daily_limit,
       min_gap_minutes = excluded.min_gap_minutes,
       updated_at = excluded.updated_at`,
  )
    .bind(
      accountKey,
      input.instagram_user_id.trim(),
      input.token_secret_name.trim(),
      input.enabled ? 1 : 0,
      dailyLimit,
      minGapMinutes,
      now,
      now,
    )
    .run();
  return json({ account_key: accountKey, enabled: Boolean(input.enabled), daily_limit: dailyLimit, min_gap_minutes: minGapMinutes }, 201);
}

async function listAccounts(env: Env): Promise<Response> {
  const rows = await env.DB.prepare(
    `SELECT account_key, instagram_user_id, token_secret_name, enabled,
            daily_limit, min_gap_minutes, created_at, updated_at
     FROM accounts ORDER BY account_key`,
  ).all();
  return json({ accounts: rows.results });
}

async function createJob(request: Request, env: Env): Promise<Response> {
  const input = await readJson<JobInput>(request);
  const accountKey = normalizeAccountKey(input.account_key);
  const account = await env.DB.prepare("SELECT account_key FROM accounts WHERE account_key = ?")
    .bind(accountKey)
    .first();
  if (!account) {
    throw new Error(`Unknown account_key: ${accountKey}`);
  }
  if (!input.external_id?.trim()) {
    throw new Error("external_id is required");
  }
  const active = await env.DB.prepare(
    `SELECT COUNT(*) AS count FROM publish_jobs
     WHERE status IN ('awaiting_upload', 'scheduled', 'processing')`,
  ).first<{ count: number }>();
  if ((active?.count || 0) >= Number(env.MAX_ACTIVE_JOBS)) {
    throw new Error("Active-job safety cap reached; cancel or finish existing jobs first");
  }
  const scheduledAt = parseScheduledAt(input.scheduled_at);
  const id = crypto.randomUUID();
  const mediaToken = crypto.randomUUID().replaceAll("-", "") + crypto.randomUUID().replaceAll("-", "");
  const mediaKey = `jobs/${accountKey}/${id}/${safeFileName(input.file_name || "reel.mp4")}`;
  const contentType = input.content_type?.trim() || "video/mp4";
  const now = nowIso();
  try {
    await env.DB.prepare(
      `INSERT INTO publish_jobs (
         id, external_id, account_key, caption, scheduled_at, status,
         media_key, media_token, content_type, next_attempt_at, created_at, updated_at
       ) VALUES (?, ?, ?, ?, ?, 'awaiting_upload', ?, ?, ?, ?, ?, ?)`,
    )
      .bind(id, input.external_id.trim(), accountKey, input.caption || "", scheduledAt, mediaKey, mediaToken, contentType, scheduledAt, now, now)
      .run();
  } catch (error) {
    if (String(error).includes("UNIQUE")) {
      throw new Error(`A job already exists for ${accountKey}/${input.external_id}`);
    }
    throw error;
  }
  return json({ id, status: "awaiting_upload", upload_path: `/v1/jobs/${id}/media`, scheduled_at: scheduledAt }, 201);
}

async function uploadMedia(request: Request, env: Env, jobId: string): Promise<Response> {
  const job = await env.DB.prepare(
    "SELECT id, status, media_key, content_type, scheduled_at FROM publish_jobs WHERE id = ?",
  )
    .bind(jobId)
    .first<{ id: string; status: string; media_key: string; content_type: string; scheduled_at: string }>();
  if (!job) {
    return json({ error: "Job not found" }, 404);
  }
  if (job.status !== "awaiting_upload") {
    return json({ error: `Job is ${job.status}; media can only be uploaded once` }, 409);
  }
  if (!request.body) {
    throw new Error("Request body is required");
  }
  const contentLength = Number(request.headers.get("content-length"));
  if (!Number.isFinite(contentLength) || contentLength <= 0) {
    throw new Error("A valid Content-Length header is required");
  }
  if (contentLength > Number(env.MAX_UPLOAD_BYTES)) {
    throw new Error("Upload exceeds the configured per-video safety cap");
  }
  const stored = await env.DB.prepare(
    "SELECT COALESCE(SUM(media_size_bytes), 0) AS bytes FROM publish_jobs WHERE media_size_bytes > 0",
  ).first<{ bytes: number }>();
  if ((stored?.bytes || 0) + contentLength > Number(env.MAX_STORED_BYTES)) {
    throw new Error("R2 storage safety cap reached; upload rejected before exceeding the free-tier buffer");
  }
  const object = await env.MEDIA.put(job.media_key, request.body, {
    httpMetadata: { contentType: request.headers.get("content-type") || job.content_type },
  });
  if (!object || object.size !== contentLength) {
    if (object) await env.MEDIA.delete(job.media_key);
    throw new Error("R2 upload size did not match Content-Length");
  }
  const now = nowIso();
  await env.DB.prepare(
    "UPDATE publish_jobs SET status = 'scheduled', media_size_bytes = ?, next_attempt_at = ?, updated_at = ? WHERE id = ? AND status = 'awaiting_upload'",
  )
    .bind(object.size, job.scheduled_at, now, jobId)
    .run();
  return json({ id: jobId, status: "scheduled" });
}

async function listJobs(env: Env): Promise<Response> {
  // DESC so the 200 most recently scheduled jobs are always returned; an ASC
  // limit would exclude today's jobs once the DB accumulates 200+ older rows.
  const rows = await env.DB.prepare(
    `SELECT id, external_id, account_key, scheduled_at, status, attempts,
            meta_container_id, meta_media_id, error_message, published_at
     FROM publish_jobs ORDER BY scheduled_at DESC LIMIT 200`,
  ).all();
  return json({ publish_mode: env.PUBLISH_MODE, jobs: rows.results });
}

async function usage(env: Env): Promise<Response> {
  const now = nowIso();
  const stored = await env.DB.prepare(
    "SELECT COALESCE(SUM(media_size_bytes), 0) AS bytes FROM publish_jobs WHERE media_size_bytes > 0",
  ).first<{ bytes: number }>();
  const placeholders = STORED_MEDIA_STATUSES.map(() => "?").join(",");
  const activeByStatusRows = await env.DB.prepare(
    `SELECT status, COUNT(*) AS count
     FROM publish_jobs WHERE status IN (${placeholders})
     GROUP BY status`,
  )
    .bind(...STORED_MEDIA_STATUSES)
    .all<StatusCountRow>();
  const oldestActive = await env.DB.prepare(
    `SELECT MIN(created_at) AS created_at
     FROM publish_jobs WHERE status IN (${placeholders})`,
  )
    .bind(...STORED_MEDIA_STATUSES)
    .first<{ created_at: string | null }>();
  const stale = await env.DB.prepare(
    `SELECT
       SUM(CASE WHEN status = 'awaiting_upload' AND created_at <= ? THEN 1 ELSE 0 END) AS awaiting_upload,
       SUM(CASE WHEN status = 'processing' AND processing_started_at <= ? THEN 1 ELSE 0 END) AS processing,
       SUM(CASE WHEN status = 'processing' AND processing_started_at IS NULL THEN 1 ELSE 0 END) AS processing_age_unknown,
       SUM(CASE WHEN status = 'scheduled' AND scheduled_at <= ? THEN 1 ELSE 0 END) AS scheduled_past_due,
       MIN(CASE WHEN status = 'scheduled' THEN scheduled_at END) AS oldest_scheduled_at
     FROM publish_jobs
     WHERE status IN (${placeholders})`,
  )
    .bind(
      addMinutes(now, -STALE_AWAITING_UPLOAD_MINUTES),
      addMinutes(now, -STALE_PROCESSING_MINUTES),
      now,
      ...STORED_MEDIA_STATUSES,
    )
    .first<{
      awaiting_upload: number | null;
      processing: number | null;
      processing_age_unknown: number | null;
      scheduled_past_due: number | null;
      oldest_scheduled_at: string | null;
    }>();
  const activeByStatus = statusCounts(activeByStatusRows.results);
  const activeJobs = Object.values(activeByStatus).reduce((total, count) => total + count, 0);
  const storedBytes = stored?.bytes || 0;
  const maxStoredBytes = Number(env.MAX_STORED_BYTES);
  const maxActiveJobs = Number(env.MAX_ACTIVE_JOBS);
  return json({
    stored_bytes: storedBytes,
    max_stored_bytes: maxStoredBytes,
    remaining_bytes: Math.max(0, maxStoredBytes - storedBytes),
    usage_percent: Number(((storedBytes / maxStoredBytes) * 100).toFixed(2)),
    active_jobs: activeJobs,
    max_active_jobs: maxActiveJobs,
    active_usage_percent: Number(((activeJobs / maxActiveJobs) * 100).toFixed(2)),
    active_jobs_by_status: activeByStatus,
    oldest_active_created_at: oldestActive?.created_at || null,
    oldest_active_age_minutes: ageMinutes(oldestActive?.created_at || null, now),
    stale_jobs: {
      awaiting_upload_over_minutes: STALE_AWAITING_UPLOAD_MINUTES,
      awaiting_upload: Number(stale?.awaiting_upload) || 0,
      processing_over_minutes: STALE_PROCESSING_MINUTES,
      processing: Number(stale?.processing) || 0,
      processing_age_unknown: Number(stale?.processing_age_unknown) || 0,
      scheduled_past_due: Number(stale?.scheduled_past_due) || 0,
      oldest_scheduled_at: stale?.oldest_scheduled_at || null,
    },
    max_upload_bytes: Number(env.MAX_UPLOAD_BYTES),
  });
}

async function cancelJob(env: Env, jobId: string): Promise<Response> {
  const job = await env.DB.prepare("SELECT media_key, status FROM publish_jobs WHERE id = ?")
    .bind(jobId)
    .first<{ media_key: string; status: string }>();
  if (!job) {
    return json({ error: "Job not found" }, 404);
  }
  if (["published", "validated"].includes(job.status)) {
    return json({ error: `Cannot cancel a ${job.status} job` }, 409);
  }
  await env.MEDIA.delete(job.media_key);
  await env.DB.prepare(
    "UPDATE publish_jobs SET status = 'canceled', media_size_bytes = 0, updated_at = ? WHERE id = ?",
  )
    .bind(nowIso(), jobId)
    .run();
  return json({ id: jobId, status: "canceled" });
}

async function forceRunJob(env: Env, jobId: string): Promise<Response> {
  const now = nowIso();
  const row = await env.DB.prepare(
    `SELECT j.*, a.instagram_user_id, a.token_secret_name, a.enabled, a.daily_limit, a.min_gap_minutes
     FROM publish_jobs j JOIN accounts a ON a.account_key = j.account_key
     WHERE j.id = ?`,
  )
    .bind(jobId)
    .first<JobRow & AccountRow>();
  if (!row) return json({ error: "Job not found" }, 404);
  if (row.status !== "scheduled") {
    return json({ error: `Only a pending 'scheduled' job can be forced (this job is ${row.status})` }, 409);
  }
  const claimed = await env.DB.prepare(
    "UPDATE publish_jobs SET lease_until = ?, updated_at = ? WHERE id = ? AND (lease_until IS NULL OR lease_until <= ?)",
  )
    .bind(addMinutes(now, 2), now, row.id, now)
    .run();
  if (!claimed.meta.changes) {
    return json({ error: "Job is currently being processed; try again in a moment" }, 409);
  }
  console.log(`force-run: operator bypassed the safety gate for job ${row.id} (account ${row.account_key})`);
  try {
    await processJob(env, row, row, { bypassGate: true });
  } catch (error) {
    if (row.status === "scheduled" && !row.meta_container_id && shouldOfferManualLocalPublish(error)) {
      await markJobForManualLocalPublish(env, row, error);
    } else if (isRecoverableMetaError(error)) {
      await deferBlockedJob(env, row, error);
    } else {
      await failJob(env, row, error);
    }
    return errorResponse(error, 400);
  }
  return json({ id: row.id, forced: true });
}

async function serveMedia(request: Request, env: Env, mediaToken: string): Promise<Response> {
  const placeholders = ACTIVE_MEDIA_STATUSES.map(() => "?").join(",");
  const job = await env.DB.prepare(
    `SELECT media_key FROM publish_jobs WHERE media_token = ? AND status IN (${placeholders})`,
  )
    .bind(mediaToken, ...ACTIVE_MEDIA_STATUSES)
    .first<{ media_key: string }>();
  if (!job) return new Response("Not found", { status: 404 });

  const head = await env.MEDIA.head(job.media_key);
  if (!head) return new Response("Not found", { status: 404 });
  const headers = new Headers({ "accept-ranges": "bytes", etag: head.httpEtag, "cache-control": "private, no-store" });
  head.writeHttpMetadata(headers);
  if (request.method === "HEAD") {
    headers.set("content-length", String(head.size));
    return new Response(null, { headers });
  }
  const range = parseRange(request.headers.get("range"), head.size);
  const object = await env.MEDIA.get(job.media_key, range ? { range } : undefined);
  if (!object) return new Response("Not found", { status: 404 });
  if (
    object.range &&
    "offset" in object.range &&
    object.range.offset !== undefined &&
    object.range.length !== undefined
  ) {
    const end = object.range.offset + object.range.length - 1;
    headers.set("content-range", `bytes ${object.range.offset}-${end}/${head.size}`);
    headers.set("content-length", String(object.range.length));
    return new Response(object.body, { status: 206, headers });
  }
  headers.set("content-length", String(head.size));
  return new Response(object.body, { headers });
}

async function graphRequest(env: Env, account: AccountRow, path: string, params: Record<string, string>, method = "GET"): Promise<Record<string, unknown>> {
  const token = env[account.token_secret_name];
  if (typeof token !== "string" || !token) {
    throw new Error(`Missing Worker secret binding: ${account.token_secret_name}`);
  }
  const allParams = new URLSearchParams({ ...params, access_token: token });
  const base = `https://${env.GRAPH_HOST}/${env.GRAPH_VERSION}/${path}`;
  const response = await fetch(method === "POST" ? base : `${base}?${allParams}`, {
    method,
    body: method === "POST" ? allParams : undefined,
  });
  const body = (await response.json()) as Record<string, unknown>;
  if (!response.ok) {
    throw new Error(`Meta HTTP ${response.status}: ${JSON.stringify(body)}`);
  }
  return body;
}

interface PublishGate {
  allowed: boolean;
  reason?: string;
  // Exact moment the gate is expected to clear, when known. Lets the caller
  // retry right at eligibility instead of a flat delay that either lags behind
  // (daily-window jobs can wait a full extra cycle) or wastes cron ticks.
  retryAt?: string;
}

async function accountMayPublish(env: Env, account: AccountRow, now: string): Promise<PublishGate> {
  if (!account.enabled) return { allowed: false, reason: "account disabled" };
  const since = addMinutes(now, -24 * 60);
  const oldestInWindow = await env.DB.prepare(
    "SELECT COUNT(*) AS count, MIN(published_at) AS oldest FROM publish_jobs WHERE account_key = ? AND published_at >= ?",
  )
    .bind(account.account_key, since)
    .first<{ count: number; oldest: string | null }>();
  const published = oldestInWindow?.count || 0;
  if (published >= account.daily_limit) {
    // The window frees up 24h after its oldest counted publish ages out.
    const retryAt = oldestInWindow?.oldest ? addMinutes(oldestInWindow.oldest, 24 * 60) : undefined;
    return {
      allowed: false,
      reason: `daily limit reached (${published}/${account.daily_limit} in 24h)`,
      retryAt,
    };
  }
  const latest = await env.DB.prepare(
    "SELECT published_at FROM publish_jobs WHERE account_key = ? AND published_at IS NOT NULL ORDER BY published_at DESC LIMIT 1",
  )
    .bind(account.account_key)
    .first<{ published_at: string }>();
  if (!latest) return { allowed: true };
  const eligibleAt = addMinutes(latest.published_at, account.min_gap_minutes);
  if (Date.parse(eligibleAt) <= Date.parse(now)) return { allowed: true };
  return { allowed: false, reason: `same-account cooldown until ${eligibleAt}`, retryAt: eligibleAt };
}

async function failJob(env: Env, job: JobRow, error: unknown): Promise<void> {
  const attempts = job.attempts + 1;
  const final = attempts >= MAX_ATTEMPTS;
  await env.DB.prepare(
    "UPDATE publish_jobs SET status = ?, attempts = ?, next_attempt_at = ?, lease_until = NULL, error_message = ?, updated_at = ? WHERE id = ?",
  )
    .bind(final ? "failed" : job.status, attempts, addMinutes(nowIso(), 15 * attempts), String(error).slice(0, 1000), nowIso(), job.id)
    .run();
  if (final) {
    await env.MEDIA.delete(job.media_key);
    await env.DB.prepare("UPDATE publish_jobs SET media_size_bytes = 0 WHERE id = ?").bind(job.id).run();
  }
}

async function deferBlockedJob(env: Env, job: JobRow, error: unknown): Promise<void> {
  // The account is blocked, not the job. Preserve the media and retry later so a multi-hour
  // Meta restriction delays posts instead of destroying them — unless the job is so far past
  // its slot that it's no longer worth keeping, in which case fail it normally (frees media).
  const now = nowIso();
  const overdue = Date.parse(now) - Date.parse(job.scheduled_at) >= BLOCK_GIVEUP_HOURS * 3_600_000;
  if (overdue) {
    await failJob(env, { ...job, attempts: MAX_ATTEMPTS - 1 }, error);
    return;
  }
  await env.DB.prepare(
    "UPDATE publish_jobs SET next_attempt_at = ?, lease_until = NULL, error_message = ?, updated_at = ? WHERE id = ?",
  )
    .bind(addMinutes(now, BLOCK_RETRY_MINUTES), `blocked, will retry: ${String(error).slice(0, 900)}`, now, job.id)
    .run();
}

async function markJobForManualLocalPublish(env: Env, job: JobRow, error: unknown): Promise<void> {
  const now = nowIso();
  await env.DB.prepare(
    `UPDATE publish_jobs
     SET status = 'manual_local_available',
         media_size_bytes = 0,
         next_attempt_at = ?,
         lease_until = NULL,
         error_message = ?,
         updated_at = ?
     WHERE id = ? AND status = 'scheduled' AND meta_container_id IS NULL`,
  )
    .bind(now, `cloud blocked; manual browser publish available: ${String(error).slice(0, 900)}`, now, job.id)
    .run();
  await env.MEDIA.delete(job.media_key);
}

async function processJob(
  env: Env,
  job: JobRow,
  account: AccountRow,
  options?: { bypassGate?: boolean },
): Promise<void> {
  const now = nowIso();
  if (job.status === "scheduled") {
    if (!options?.bypassGate) {
      const gate = await accountMayPublish(env, account, now);
      if (!gate.allowed) {
        // Prefer the gate's computed eligibility moment (never earlier than 1 min
        // out, to avoid hammering); fall back to the flat +15 min when the gate
        // didn't return one (e.g. "account disabled" has no known clear time).
        const fallback = addMinutes(now, 15);
        const nextAttemptAt = gate.retryAt
          ? new Date(Math.max(Date.parse(gate.retryAt), Date.parse(addMinutes(now, 1)))).toISOString()
          : fallback;
        await env.DB.prepare(
          "UPDATE publish_jobs SET next_attempt_at = ?, lease_until = NULL, error_message = ?, updated_at = ? WHERE id = ?",
        )
          .bind(nextAttemptAt, gate.reason ?? "blocked", now, job.id)
          .run();
        return;
      }
    } else if (!account.enabled) {
      // bypassGate skips the daily_limit/min_gap cooldown, never the enabled floor.
      throw new Error("account disabled");
    }
    const baseUrl = env.PUBLIC_BASE_URL.replace(/\/$/, "");
    if (!baseUrl) throw new Error("PUBLIC_BASE_URL is required before validate/live mode");
    const created = await graphRequest(
      env,
      account,
      `${account.instagram_user_id}/media`,
      { media_type: "REELS", video_url: `${baseUrl}/media/${job.media_token}`, caption: job.caption },
      "POST",
    );
    const containerId = String(created.id || "");
    if (!containerId) throw new Error("Meta did not return a container id");
    await env.DB.prepare(
      "UPDATE publish_jobs SET status = 'processing', meta_container_id = ?, processing_started_at = ?, next_attempt_at = ?, lease_until = NULL, updated_at = ? WHERE id = ?",
    )
      .bind(containerId, now, addMinutes(now, 1), now, job.id)
      .run();
    return;
  }

  if (job.status === "processing" && job.meta_container_id) {
    const snapshot = await graphRequest(env, account, job.meta_container_id, { fields: "status_code,status" });
    const status = String(snapshot.status_code || "");
    if (status === "PUBLISHED") {
      // A previous media_publish call landed server-side even though its HTTP
      // response failed (Meta can 500 AFTER the publish took effect). Without
      // this branch such a job re-polls forever or ends "failed" while the
      // reel is live. The media id was lost with that failed response.
      await recordPublished(env, job, "", now);
      return;
    }
    if (status === "FINISHED") {
      if (env.PUBLISH_MODE === "validate") {
        await env.DB.prepare(
          "UPDATE publish_jobs SET status = 'validated', media_size_bytes = 0, lease_until = NULL, updated_at = ? WHERE id = ?",
        )
          .bind(now, job.id)
          .run();
        await env.MEDIA.delete(job.media_key);
        return;
      }
      let published: Record<string, unknown>;
      try {
        published = await graphRequest(env, account, `${account.instagram_user_id}/media_publish`, { creation_id: job.meta_container_id }, "POST");
      } catch (error) {
        // The publish may have succeeded despite the error response — check the
        // container before counting a failure, so the job can never read
        // "failed" while the reel is actually live.
        const verify = await graphRequest(env, account, job.meta_container_id, { fields: "status_code" }).catch(() => null);
        if (!verify || String(verify.status_code || "") !== "PUBLISHED") throw error;
        published = {};
      }
      await recordPublished(env, job, String(published.id || ""), now);
      return;
    }
    if (["ERROR", "EXPIRED"].includes(status)) throw new Error(`Meta container ended with ${status}`);
    await env.DB.prepare("UPDATE publish_jobs SET next_attempt_at = ?, lease_until = NULL, updated_at = ? WHERE id = ?")
      .bind(addMinutes(now, 1), now, job.id)
      .run();
  }
}

async function recordPublished(env: Env, job: JobRow, metaMediaId: string, now: string): Promise<void> {
  await env.DB.prepare(
    "UPDATE publish_jobs SET status = 'published', media_size_bytes = 0, meta_media_id = ?, published_at = ?, lease_until = NULL, updated_at = ? WHERE id = ?",
  )
    .bind(metaMediaId, now, now, job.id)
    .run();
  await env.MEDIA.delete(job.media_key);
}

async function processDueJobs(env: Env): Promise<{ processed: number; mode: string }> {
  if (env.PUBLISH_MODE === "disabled") return { processed: 0, mode: env.PUBLISH_MODE };
  const now = nowIso();
  const rows = await env.DB.prepare(
    `SELECT j.*, a.instagram_user_id, a.token_secret_name, a.enabled, a.daily_limit, a.min_gap_minutes
     FROM publish_jobs j JOIN accounts a ON a.account_key = j.account_key
     WHERE j.status IN ('scheduled', 'processing')
       AND j.next_attempt_at <= ?
       AND (j.lease_until IS NULL OR j.lease_until <= ?)
     ORDER BY j.next_attempt_at ASC LIMIT ?`,
  )
    .bind(now, now, MAX_JOBS_PER_TICK)
    .all<JobRow & AccountRow>();

  let processed = 0;
  for (const row of rows.results) {
    const claimed = await env.DB.prepare(
      "UPDATE publish_jobs SET lease_until = ?, updated_at = ? WHERE id = ? AND (lease_until IS NULL OR lease_until <= ?)",
    )
      .bind(addMinutes(now, 2), now, row.id, now)
      .run();
    if (!claimed.meta.changes) continue;
    try {
      await processJob(env, row, row);
      processed += 1;
    } catch (error) {
      if (row.status === "scheduled" && !row.meta_container_id && shouldOfferManualLocalPublish(error)) {
        await markJobForManualLocalPublish(env, row, error);
      } else if (isRecoverableMetaError(error)) {
        await deferBlockedJob(env, row, error);
      } else {
        await failJob(env, row, error);
      }
    }
  }
  return { processed, mode: env.PUBLISH_MODE };
}

const LEGAL_UPDATED = "2026-06-16";
const LEGAL_CONTACT = "jeremywijaya81@gmail.com";

function legalPage(title: string, bodyHtml: string): Response {
  const html = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NicheFlow Studio — ${title}</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 16px/1.6 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
         max-width: 720px; margin: 0 auto; padding: 2.5rem 1.25rem; }
  h1 { font-size: 1.6rem; margin-bottom: .25rem; }
  h2 { font-size: 1.1rem; margin-top: 2rem; }
  .meta { color: #6b7280; font-size: .9rem; }
  a { color: #6d28d9; }
</style></head>
<body>
<h1>NicheFlow Studio — ${title}</h1>
<p class="meta">Last updated ${LEGAL_UPDATED} · Contact: <a href="mailto:${LEGAL_CONTACT}">${LEGAL_CONTACT}</a></p>
${bodyHtml}
</body></html>`;
  return new Response(html, { status: 200, headers: { "content-type": "text/html; charset=utf-8" } });
}

const PRIVACY_BODY = `
<p>NicheFlow Studio ("the App") is a personal content-publishing tool operated by an
individual to publish their own short-form videos (Reels) to their own Instagram accounts
through the Meta Graph API. This policy explains what data the App handles.</p>
<h2>What we handle</h2>
<ul>
  <li><strong>Instagram account credentials</strong> — access tokens and account IDs for Instagram accounts the operator owns, used only to publish on the operator's behalf.</li>
  <li><strong>Content the operator creates</strong> — video files and captions queued for publishing.</li>
  <li><strong>Operational metadata</strong> — scheduling times, publish status, and error logs.</li>
</ul>
<h2>What we do not do</h2>
<ul>
  <li>We do not collect data from the public or from third-party users.</li>
  <li>We do not sell, rent, or share personal data for advertising.</li>
</ul>
<h2>Storage and retention</h2>
<p>Access tokens are stored as encrypted Cloudflare Worker secrets. Video files are held
temporarily in Cloudflare R2 only until publishing completes, then deleted. Minimal job
metadata is kept in Cloudflare D1 for operational history.</p>
<h2>Sharing</h2>
<p>Data is shared only with Meta (to publish the operator's content) and Cloudflare (hosting
infrastructure). No other third parties.</p>
<h2>Your choices</h2>
<p>The operator can revoke the App's access at any time from their Instagram/Meta account
settings, which stops all publishing. See <a href="/data-deletion">Data Deletion</a>.</p>`;

const TERMS_BODY = `
<p>NicheFlow Studio is provided for the operator's own personal use, "as is", without warranty.</p>
<h2>Acceptable use</h2>
<p>The operator is solely responsible for the content they publish and for complying with
Instagram's and Meta's Platform Terms and Community Guidelines.</p>
<h2>Liability</h2>
<p>The App is a personal automation tool; the operator assumes all risk arising from its use,
including any action taken by Meta on connected accounts.</p>`;

const DATA_DELETION_BODY = `
<p>NicheFlow Studio only processes data belonging to its operator (their own Instagram accounts
and the content they create). To delete that data:</p>
<ol>
  <li>Revoke the App's access from your Instagram/Meta account settings (Business Integrations), which immediately invalidates its tokens.</li>
  <li>Email <a href="mailto:${LEGAL_CONTACT}">${LEGAL_CONTACT}</a> to request deletion of any stored tokens, queued media, and job metadata.</li>
</ol>
<p>Requests are honored within 30 days. Queued video files are in any case deleted automatically
once publishing completes.</p>`;

async function route(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  if (request.method === "GET" && url.pathname === "/health") {
    return json({ ok: true, publish_mode: env.PUBLISH_MODE });
  }
  const mediaMatch = url.pathname.match(/^\/media\/([a-f0-9]+)$/);
  if (mediaMatch && ["GET", "HEAD"].includes(request.method)) {
    return serveMedia(request, env, mediaMatch[1]);
  }
  // Public legal pages (no API key) — Meta and end users must be able to fetch these.
  if (request.method === "GET" && url.pathname === "/privacy") return legalPage("Privacy Policy", PRIVACY_BODY);
  if (request.method === "GET" && url.pathname === "/terms") return legalPage("Terms of Service", TERMS_BODY);
  if (request.method === "GET" && url.pathname === "/data-deletion") return legalPage("Data Deletion", DATA_DELETION_BODY);

  requireAuth(request, env);
  if (request.method === "PUT" && url.pathname === "/v1/accounts") return upsertAccount(request, env);
  if (request.method === "GET" && url.pathname === "/v1/accounts") return listAccounts(env);
  if (request.method === "POST" && url.pathname === "/v1/jobs") return createJob(request, env);
  if (request.method === "GET" && url.pathname === "/v1/jobs") return listJobs(env);
  if (request.method === "GET" && url.pathname === "/v1/usage") return usage(env);
  if (request.method === "POST" && url.pathname === "/v1/run") return json(await processDueJobs(env));
  const uploadMatch = url.pathname.match(/^\/v1\/jobs\/([^/]+)\/media$/);
  if (request.method === "PUT" && uploadMatch) return uploadMedia(request, env, uploadMatch[1]);
  const cancelMatch = url.pathname.match(/^\/v1\/jobs\/([^/]+)\/cancel$/);
  if (request.method === "POST" && cancelMatch) return cancelJob(env, cancelMatch[1]);
  const forceRunMatch = url.pathname.match(/^\/v1\/jobs\/([^/]+)\/force-run$/);
  if (request.method === "POST" && forceRunMatch) return forceRunJob(env, forceRunMatch[1]);
  return json({ error: "Not found" }, 404);
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    try {
      return await route(request, env);
    } catch (error) {
      if (error instanceof Response) return error;
      console.error(error);
      return errorResponse(error, 400);
    }
  },
  async scheduled(_controller: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(processDueJobs(env));
  },
} satisfies ExportedHandler<Env>;
