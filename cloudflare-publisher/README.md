# NicheFlow Cloudflare Publisher

Free-tier-first, multi-account Instagram Graph API publishing service.

The service is intentionally deployed with `PUBLISH_MODE=disabled` first:

- `disabled`: stores schedules and videos, but makes no Meta API calls.
- `validate`: creates and polls Reel containers, but never calls `media_publish`.
- `live`: publishes finished containers. Enable only after validation.

## Resources

- One Worker with a one-minute Cron Trigger.
- One private R2 bucket: `nicheflow-publish-media`.
- One D1 database: `nicheflow-publisher`.
- One `API_KEY` Worker secret for NicheFlow-to-Worker authentication.
- One Worker secret per Instagram account token, for example
  `IG_TOKEN_PASTMOMENTSDAILY`.

Instagram tokens are never stored in D1.

## Free-plan boundaries

- Uploads pass through the Worker and must remain below Cloudflare's 100 MB
  request-body limit on the Free plan.
- The Worker rejects videos above 95 MB.
- The Worker rejects uploads once tracked R2 storage would exceed 8 GB, leaving
  a 2 GB safety margin below R2's 10 GB free monthly storage allowance.
- The Worker accepts at most 150 active jobs at once.
- The scheduler processes at most five state transitions per minute.
- Each account defaults to six posts per rolling 24 hours and a 240-minute
  minimum gap.
- R2 media is deleted after a job is validated or published.
- Final failures and canceled jobs also delete their R2 media.

These are application-side safety limits. They prevent this Worker from
crossing the configured storage cap, but Cloudflare does not provide a
guaranteed account-wide hard billing cutoff. Do not manually upload unrelated
objects into the bucket.

Authenticated usage status is available at:

```text
GET /v1/usage
```

The response includes tracked R2 storage, active-job cap utilization, active
counts by status, oldest active-job age, and warning counts for uploads or Meta
processing jobs older than two hours. Past-due scheduled jobs are reported for
inspection but are not automatically deleted because account limits can
legitimately defer them.

## Initial setup

From this directory:

```powershell
npm install
npx wrangler login
npx wrangler d1 create nicheflow-publisher
npx wrangler r2 bucket create nicheflow-publish-media
```

Copy the returned D1 `database_id` into `wrangler.jsonc`, then initialize it:

```powershell
npm run db:remote
npx wrangler secret put API_KEY
npm run deploy
```

After deployment, copy the Worker URL into `PUBLIC_BASE_URL` in
`wrangler.jsonc`, then deploy again.

For each account, add its token as a separate encrypted Worker secret:

```powershell
npx wrangler secret put IG_TOKEN_PASTMOMENTSDAILY
```

Do not put secret values directly in commands or `wrangler.jsonc`; Wrangler
will prompt securely.

## Register an account

Easiest path — the repo's helper reads `IG_USER_ID_<ACCOUNT>` from `.env`, runs a
read-only token check (confirms the token works and the account is Professional),
then registers it. Run it from the repo root after setting the Worker secret:

```powershell
.venv\Scripts\python.exe scripts\cloudflare_register_account.py beneathhistory --daily-limit 3
```

Or call the API directly. Keep `enabled` false until its token and user ID are verified:

```powershell
$headers = @{ Authorization = "Bearer <API_KEY>" }
$body = @{
  account_key = "pastmomentsdaily"
  instagram_user_id = "<IG_USER_ID>"
  token_secret_name = "IG_TOKEN_PASTMOMENTSDAILY"
  enabled = $false
  daily_limit = 6
  min_gap_minutes = 240
} | ConvertTo-Json

Invoke-RestMethod -Method Put -Uri "<WORKER_URL>/v1/accounts" `
  -Headers $headers -ContentType "application/json" -Body $body
```

## Queue and upload a Reel

Create the job first, then stream the MP4 to R2:

```powershell
$job = @{
  external_id = "local-upload-job-123"
  account_key = "pastmomentsdaily"
  caption = "Caption text"
  scheduled_at = "2026-06-15T02:00:00Z"
  file_name = "reel.mp4"
  content_type = "video/mp4"
} | ConvertTo-Json

$created = Invoke-RestMethod -Method Post -Uri "<WORKER_URL>/v1/jobs" `
  -Headers $headers -ContentType "application/json" -Body $job

Invoke-RestMethod -Method Put -Uri ("<WORKER_URL>" + $created.upload_path) `
  -Headers $headers -ContentType "video/mp4" -InFile "C:\path\to\reel.mp4"
```

## Safe rollout

1. Deploy with `PUBLISH_MODE=disabled`.
2. Register one account with `enabled=false`.
3. Verify authenticated job creation, upload, listing, and cancel.
4. Set `PUBLISH_MODE=validate`, enable one account, and queue one test job.
5. Confirm it reaches `validated`; nothing is posted.
6. Perform one explicitly approved live publish.
7. Expand account by account.

After deployment and secret setup, the repository validation helper performs
steps 4-5 and refuses to run in `live` mode:

```powershell
.\.venv\Scripts\python.exe ..\scripts\cloudflare_publish_validate.py `
  --account pastmomentsdaily `
  --video "C:\path\to\approved-reel.mp4"
```
