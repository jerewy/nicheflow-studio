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
const result = document.querySelector("#result");

function show(tone, message) {
  result.dataset.tone = tone;
  result.textContent = message;
}

function renderDashboard(dashboard) {
  const selected = dashboard?.pools?.[niche.value];
  const usage = dashboard?.apify_usage;
  poolLabel.textContent = `${niche.value[0].toUpperCase()}${niche.value.slice(1)} pool`;
  poolCount.textContent = selected?.video_count ?? "Unavailable";
  apifyCost.textContent =
    typeof usage?.estimated_cost_usd === "number" ? `$${usage.estimated_cost_usd.toFixed(4)}` : "Unavailable";
  apifyResults.textContent = usage ? `${usage.used} / ${usage.free_cap}` : "Unavailable";
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
    if (!chrome.runtime.lastError && response?.ok) renderDashboard(response.dashboard);
  });
}

async function renderQueueState() {
  const stored = await chrome.storage.local.get({ captureQueue: [], processing: false, lastBatch: null });
  queueCount.textContent = stored.captureQueue.length;
  queueButton.disabled = stored.processing;
  processButton.disabled = stored.processing || !stored.captureQueue.length;
  processButton.textContent = stored.processing ? "Processing..." : "Process Queue";
  if (stored.processing) {
    show("neutral", "Batch is running in the background. You can close this popup.");
  } else if (stored.lastBatch?.ok) {
    const summary = stored.lastBatch.summary;
    show("success", `${summary.added} added, ${summary.duplicates} duplicate, ${summary.failed} failed.`);
    renderDashboard(stored.lastBatch.dashboard);
  } else if (stored.lastBatch?.error) {
    show("error", stored.lastBatch.error);
  }
}

async function queueCurrent() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.url || !tab.url.includes("instagram.com/")) {
    show("error", "Open an Instagram Reel or post first.");
    return;
  }
  const stored = await chrome.storage.local.get({ captureQueue: [] });
  const shortcode = tab.url.split("?")[0].replace(/\/$/, "");
  if (stored.captureQueue.some((item) => item.url.split("?")[0].replace(/\/$/, "") === shortcode)) {
    show("error", "This Reel is already queued.");
    return;
  }
  stored.captureQueue.push({
    url: tab.url,
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
}

queueButton.addEventListener("click", queueCurrent);
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
