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

async function processQueue() {
  const stored = await chrome.storage.local.get({ captureQueue: [], processing: false });
  if (stored.processing) return { ok: false, error: "The queue is already processing." };
  if (!stored.captureQueue.length) return { ok: false, error: "The capture queue is empty." };

  await chrome.storage.local.set({ processing: true, lastBatch: null });
  await updateBadge();
  chrome.runtime.sendNativeMessage(
    HOST_NAME,
    { action: "capture_batch", items: stored.captureQueue },
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
      await chrome.storage.local.set({
        captureQueue: [],
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

chrome.runtime.onInstalled.addListener(updateBadge);
chrome.runtime.onStartup.addListener(updateBadge);
