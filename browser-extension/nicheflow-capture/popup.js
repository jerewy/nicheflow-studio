const HOST_NAME = "com.nicheflow.capture";
const niche = document.querySelector("#niche");
const pinnedAccount = document.querySelector("#pinned-account");
const queueButton = document.querySelector("#queue");
const processButton = document.querySelector("#process");
const queueCount = document.querySelector("#queue-count");
const poolLabel = document.querySelector("#pool-label");
const poolCount = document.querySelector("#pool-count");
const apifyCost = document.querySelector("#apify-cost");
const apifyResults = document.querySelector("#apify-results");
const queueList = document.querySelector("#queue-list");
const queueClear = document.querySelector("#queue-clear");
const result = document.querySelector("#result");
const captureUrlHelpers = globalThis.NicheFlowCaptureUrl;

function show(tone, message) {
  result.dataset.tone = tone;
  result.textContent = message;
}

function renderStats(dashboard) {
  const selected = dashboard?.pools?.[niche.value];
  const usage = dashboard?.apify_usage;
  poolLabel.textContent = `${niche.value[0].toUpperCase()}${niche.value.slice(1)} pool`;
  poolCount.textContent = selected?.video_count ?? "Unavailable";
  apifyCost.textContent =
    typeof usage?.estimated_cost_usd === "number" ? `$${usage.estimated_cost_usd.toFixed(4)}` : "Unavailable";
  apifyResults.textContent = usage ? `${usage.used} / ${usage.free_cap}` : "Unavailable";
}

// Accounts are populated ONLY from a fresh get_dashboard (loadDashboard), never
// from a saved batch snapshot — a stale snapshot taken before an account's niche
// was set would otherwise wipe the pin list back to "No pin".
function renderAccounts(dashboard) {
  const selected = dashboard?.pools?.[niche.value];
  const previousPin = pinnedAccount.value;
  pinnedAccount.replaceChildren(new Option("No pin", ""));
  for (const account of selected?.accounts ?? []) {
    pinnedAccount.add(new Option(account.name, String(account.id)));
  }
  pinnedAccount.value = [...pinnedAccount.options].some((option) => option.value === previousPin)
    ? previousPin
    : "";
}

function loadDashboard() {
  chrome.runtime.sendNativeMessage(HOST_NAME, { action: "get_dashboard" }, (response) => {
    if (chrome.runtime.lastError) {
      show("error", `Can't reach NicheFlow host: ${chrome.runtime.lastError.message}. Is the desktop app set up?`);
      return;
    }
    if (!response?.ok) {
      show("error", response?.error ?? "NicheFlow host returned no dashboard.");
      return;
    }
    renderStats(response.dashboard);
    renderAccounts(response.dashboard);
  });
}

// The Reel shortcode (last path segment) is the most recognizable label.
function shortLabel(url) {
  try {
    const parts = new URL(url).pathname.split("/").filter(Boolean);
    return parts.length ? parts[parts.length - 1] : url;
  } catch {
    return url;
  }
}

// Resolve a pinned account id to its name via the (freshly loaded) pin options.
function accountName(id) {
  if (id == null) return null;
  const option = [...pinnedAccount.options].find((o) => o.value === String(id));
  return option ? option.textContent : `#${id}`;
}

function renderQueueList(queue) {
  queueClear.disabled = !queue.length;
  if (!queue.length) {
    queueList.replaceChildren(
      Object.assign(document.createElement("div"), {
        className: "qempty",
        textContent: "Queue is empty. Open a Reel and click Queue Current.",
      }),
    );
    return;
  }
  queueList.replaceChildren();
  for (const item of queue) {
    const row = document.createElement("div");
    row.className = "qitem";
    const label = document.createElement("span");
    label.className = "qlabel";
    label.title = item.url;
    label.textContent = `${shortLabel(item.url)} · ${item.niche}`;
    if (item.pinned_account_id != null) {
      const pin = document.createElement("span");
      pin.className = "qpin";
      pin.textContent = ` · 📌 ${accountName(item.pinned_account_id)}`;
      label.append(pin);
    }
    const remove = document.createElement("button");
    remove.className = "qremove";
    remove.type = "button";
    remove.textContent = "×";
    remove.title = "Remove from queue";
    remove.addEventListener("click", () => removeFromQueue(item.url));
    row.append(label, remove);
    queueList.append(row);
  }
}

async function removeFromQueue(url) {
  const stored = await chrome.storage.local.get({ captureQueue: [] });
  const remaining = stored.captureQueue.filter((item) => item.url !== url);
  await chrome.storage.local.set({ captureQueue: remaining });
  chrome.runtime.sendMessage({ action: "refresh_badge" });
  renderQueueState();
}

async function clearQueue() {
  await chrome.storage.local.set({ captureQueue: [] });
  chrome.runtime.sendMessage({ action: "refresh_badge" });
  renderQueueState();
}

// A batch whose worker was killed (MV3 can terminate it) would otherwise leave
// `processing` stuck true and lock the buttons; treat it as finished after this.
const PROCESSING_STALE_MS = 10 * 60 * 1000;

async function renderQueueState() {
  const stored = await chrome.storage.local.get({
    captureQueue: [],
    processing: false,
    processingStartedAt: 0,
    lastBatch: null,
  });
  const staleProcessing =
    stored.processing && Date.now() - (stored.processingStartedAt || 0) > PROCESSING_STALE_MS;
  const activeProcessing = stored.processing && !staleProcessing;
  queueCount.textContent = stored.captureQueue.length;
  renderQueueList(stored.captureQueue);
  // Queueing stays available during a batch so you can keep capturing Reels.
  queueButton.disabled = false;
  processButton.disabled = activeProcessing || !stored.captureQueue.length;
  processButton.textContent = activeProcessing ? "Processing..." : "Process Queue";
  if (activeProcessing) {
    show("neutral", "Batch is running in the background — you can keep queueing Reels.");
  } else if (staleProcessing) {
    show("error", "The last batch didn't report back. You can process again.");
  } else if (stored.lastBatch?.ok) {
    const summary = stored.lastBatch.summary;
    show("success", `${summary.added} added, ${summary.duplicates} duplicate, ${summary.failed} failed.`);
    renderStats(stored.lastBatch.dashboard);
  } else if (stored.lastBatch?.error) {
    show("error", stored.lastBatch.error);
  }
}

async function queueCurrent() {
  try {
    const mediaUrl = await captureUrlHelpers.getCurrentInstagramMediaUrl((options) =>
      chrome.tabs.query(options),
    );
    if (!mediaUrl) {
      show("error", "Open an Instagram Reel or post first.");
      return;
    }
    const stored = await chrome.storage.local.get({ captureQueue: [] });
    if (
      stored.captureQueue.some(
        (item) => captureUrlHelpers.normalizeInstagramMediaUrl(item.url) === mediaUrl,
      )
    ) {
      show("error", "This Reel is already queued.");
      return;
    }
    stored.captureQueue.push({
      url: mediaUrl,
      niche: niche.value,
      pinned_account_id: pinnedAccount.value ? Number(pinnedAccount.value) : null,
      queuedAt: new Date().toISOString(),
    });
    await chrome.storage.local.set({ captureQueue: stored.captureQueue, lastBatch: null });
    await chrome.storage.sync.set({ niche: niche.value });
    chrome.runtime.sendMessage({ action: "refresh_badge" });
    show(
      "success",
      pinnedAccount.value
        ? `Queued for the ${niche.value} pool, pinned to ${pinnedAccount.selectedOptions[0].textContent}.`
        : `Queued for the ${niche.value} pool.`,
    );
    await renderQueueState();
  } catch (error) {
    show("error", `Could not queue this Reel: ${error?.message ?? error}`);
  }
}

queueButton.addEventListener("click", queueCurrent);
queueClear.addEventListener("click", clearQueue);
processButton.addEventListener("click", () => {
  chrome.runtime.sendMessage({ action: "process_queue" }, (response) => {
    if (!response?.ok) show("error", response?.error ?? "Could not start the batch.");
    renderQueueState();
  });
});
niche.addEventListener("change", () => {
  chrome.storage.sync.set({ niche: niche.value });
  loadDashboard();
});
chrome.storage.onChanged.addListener((_changes, area) => {
  if (area === "local") renderQueueState();
});
chrome.storage.sync.get({ niche: "history" }).then((stored) => {
  niche.value = stored.niche;
  loadDashboard();
  renderQueueState();
});
