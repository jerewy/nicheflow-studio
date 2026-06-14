const HOST_NAME = "com.nicheflow.capture";

function notify(title, message) {
  chrome.notifications.create({
    type: "basic",
    iconUrl: "icon128.png",
    title,
    message,
  });
}

async function updateBadge() {
  const stored = await chrome.storage.local.get({ captureQueue: [], processing: false });
  const text = stored.processing ? "..." : stored.captureQueue.length ? String(stored.captureQueue.length) : "";
  await chrome.action.setBadgeBackgroundColor({ color: stored.processing ? "#334155" : "#2563eb" });
  await chrome.action.setBadgeText({ text });
}

// Keep in sync with popup.js: a batch whose worker was killed is treated as
// finished after this, so a stuck `processing` flag can't lock out re-processing.
const PROCESSING_STALE_MS = 10 * 60 * 1000;

async function processQueue() {
  const stored = await chrome.storage.local.get({
    captureQueue: [],
    processing: false,
    processingStartedAt: 0,
  });
  const stale =
    stored.processing && Date.now() - (stored.processingStartedAt || 0) > PROCESSING_STALE_MS;
  if (stored.processing && !stale) return { ok: false, error: "The queue is already processing." };
  if (!stored.captureQueue.length) return { ok: false, error: "The capture queue is empty." };

  // Snapshot what we send so items queued DURING the batch survive completion.
  const batchItems = stored.captureQueue.slice();
  const batchUrls = new Set(batchItems.map((item) => item.url));
  await chrome.storage.local.set({
    processing: true,
    processingStartedAt: Date.now(),
    lastBatch: null,
  });
  await updateBadge();
  chrome.runtime.sendNativeMessage(
    HOST_NAME,
    { action: "capture_batch", items: batchItems },
    async (response) => {
      if (chrome.runtime.lastError || !response?.ok) {
        const error = chrome.runtime.lastError?.message ?? response?.error ?? "Unknown error.";
        await chrome.storage.local.set({ processing: false, lastBatch: { ok: false, error } });
        await chrome.action.setBadgeBackgroundColor({ color: "#b91c1c" });
        await chrome.action.setBadgeText({ text: "!" });
        notify("NicheFlow batch failed", error);
        return;
      }
      const summary = response.batch.summary;
      // Remove only the items we just processed; keep anything queued meanwhile.
      const current = await chrome.storage.local.get({ captureQueue: [] });
      const remaining = current.captureQueue.filter((item) => !batchUrls.has(item.url));
      await chrome.storage.local.set({
        captureQueue: remaining,
        processing: false,
        lastBatch: { ok: true, ...response.batch },
      });
      await chrome.action.setBadgeBackgroundColor({ color: "#15803d" });
      await chrome.action.setBadgeText({ text: String(summary.added) });
      setTimeout(updateBadge, 6000);
      notify(
        "NicheFlow batch complete",
        `${summary.added} added, ${summary.duplicates} duplicate, ${summary.failed} failed.`,
      );
    },
  );
  return { ok: true };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.action === "process_queue") {
    processQueue().then(sendResponse);
    return true;
  }
  if (message?.action === "refresh_badge") {
    updateBadge().then(() => sendResponse({ ok: true }));
    return true;
  }
  return false;
});

// Clicking the toolbar icon opens the docked Side Panel (stays open while you
// click around Instagram) instead of a popup that closes on blur.
chrome.runtime.onInstalled.addListener(() => {
  if (chrome.sidePanel?.setPanelBehavior) {
    chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
  }
  updateBadge();
});
chrome.runtime.onStartup.addListener(() => {
  if (chrome.sidePanel?.setPanelBehavior) {
    chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
  }
  updateBadge();
});
