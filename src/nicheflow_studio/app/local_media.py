"""Local media URLs for the Processing webview."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

from nicheflow_studio.core.paths import data_dir

MEDIA_HOST = "nicheflow-data.local"
MEDIA_ORIGIN = f"https://{MEDIA_HOST}"


def media_url(path: str | None) -> str | None:
    """Return a virtual-origin URL for a file inside the app data directory."""
    if not path:
        return None

    root = data_dir().resolve()
    candidate = Path(path).expanduser().resolve()
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return None
    return f"{MEDIA_ORIGIN}/{quote(relative.as_posix(), safe='/')}"


def install_windows_mapping(window) -> None:
    """Map the app data directory into a running WebView2 window."""
    if os.name != "nt":
        return

    from Microsoft.Web.WebView2.Core import CoreWebView2HostResourceAccessKind
    from System import Action

    native = window.native

    def install() -> None:
        native.webview.CoreWebView2.SetVirtualHostNameToFolderMapping(
            MEDIA_HOST,
            str(data_dir().resolve()),
            CoreWebView2HostResourceAccessKind.Allow,
        )

    native.Invoke(Action(install))
