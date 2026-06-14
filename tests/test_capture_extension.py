from __future__ import annotations

import json
import subprocess
from pathlib import Path


EXTENSION_DIR = Path(__file__).resolve().parents[1] / "browser-extension" / "nicheflow-capture"


def _normalize(url: str) -> str | None:
    script = (
        f"require({json.dumps(str(EXTENSION_DIR / 'media-url.js'))});"
        f"console.log(JSON.stringify(globalThis.NicheFlowCaptureUrl.normalizeInstagramMediaUrl("
        f"{json.dumps(url)})));"
    )
    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _current_media_url(tab_url: str) -> dict:
    script = f"""
require({json.dumps(str(EXTENSION_DIR / 'media-url.js'))});
(async () => {{
  const calls = [];
  const url = await globalThis.NicheFlowCaptureUrl.getCurrentInstagramMediaUrl(
    async (options) => {{
      calls.push(options);
      return [{{ url: {json.dumps(tab_url)} }}];
    }},
  );
  console.log(JSON.stringify({{ calls, url }}));
}})();
"""
    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _compile_popup_scripts_together() -> None:
    script = f"""
const fs = require("fs");
const vm = require("vm");
const sources = [
  fs.readFileSync({json.dumps(str(EXTENSION_DIR / "media-url.js"))}, "utf8"),
  fs.readFileSync({json.dumps(str(EXTENSION_DIR / "popup.js"))}, "utf8"),
];
new vm.Script(sources.join("\\n"));
"""
    subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)


def _compile_popup_with_legacy_helper_scope() -> None:
    script = f"""
const fs = require("fs");
const vm = require("vm");
const legacyHelper = `
function normalizeInstagramMediaUrl(value) {{ return value; }}
async function getCurrentInstagramMediaUrl(queryTabs) {{ return null; }}
globalThis.NicheFlowCaptureUrl = {{
  getCurrentInstagramMediaUrl,
  normalizeInstagramMediaUrl,
}};
`;
const popup = fs.readFileSync({json.dumps(str(EXTENSION_DIR / "popup.js"))}, "utf8");
new vm.Script(legacyHelper + "\\n" + popup);
"""
    subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)


def test_manifest_grants_only_instagram_host_access() -> None:
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["host_permissions"] == ["https://www.instagram.com/*"]
    assert "tabs" not in manifest["permissions"]


def test_capture_url_normalizes_media_routes() -> None:
    assert (
        _normalize("https://www.instagram.com/reels/ABC123/?utm_source=test")
        == "https://www.instagram.com/reel/ABC123/"
    )
    assert _normalize("https://www.instagram.com/p/POST123/") == (
        "https://www.instagram.com/p/POST123/"
    )


def test_capture_url_rejects_profiles_and_other_hosts() -> None:
    assert _normalize("https://www.instagram.com/insidehistory/") is None
    assert _normalize("https://instagram.com/reel/ABC123/") is None


def test_side_panel_queries_the_last_focused_window_for_current_media() -> None:
    result = _current_media_url("https://www.instagram.com/reels/ABC123/?utm_source=test")

    assert result == {
        "calls": [{"active": True, "lastFocusedWindow": True}],
        "url": "https://www.instagram.com/reel/ABC123/",
    }


def test_popup_scripts_share_a_page_without_global_declaration_collisions() -> None:
    _compile_popup_scripts_together()


def test_popup_is_compatible_with_a_legacy_helper_page_context() -> None:
    _compile_popup_with_legacy_helper_scope()
