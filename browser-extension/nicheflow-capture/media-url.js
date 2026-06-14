(function exposeCaptureUrlHelpers() {
  function normalizeInstagramMediaUrl(value) {
    try {
      const url = new URL(value);
      if (url.protocol !== "https:" || url.hostname !== "www.instagram.com") return null;
      const parts = url.pathname.split("/").filter(Boolean);
      if (parts.length < 2 || !["reel", "reels", "p", "tv"].includes(parts[0])) return null;
      const kind = parts[0] === "reels" ? "reel" : parts[0];
      return `https://www.instagram.com/${kind}/${parts[1]}/`;
    } catch {
      return null;
    }
  }

  async function getCurrentInstagramMediaUrl(queryTabs) {
    const [tab] = await queryTabs({ active: true, lastFocusedWindow: true });
    return normalizeInstagramMediaUrl(tab?.url);
  }

  globalThis.NicheFlowCaptureUrl = {
    getCurrentInstagramMediaUrl,
    normalizeInstagramMediaUrl,
  };
})();
