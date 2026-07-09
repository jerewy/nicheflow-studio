"""Windows OS clipboard access via ctypes (no extra dependency).

The React Processing window cannot rely on ``navigator.clipboard``: WebView2
rejects those calls with "Document is not focused" whenever the window has
lost focus — which is exactly what happens when a copy lands after a long
async step (batch prepare, downloads) and the user has already alt-tabbed to
ChatGPT/Claude. The Win32 clipboard has no focus requirement and is callable
from any thread, so the pywebview bridge routes clipboard reads/writes
through this module instead.
"""

from __future__ import annotations

import ctypes
import sys
import time

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

_OPEN_RETRIES = 10
_OPEN_RETRY_DELAY_SECONDS = 0.02


class ClipboardError(RuntimeError):
    """The OS clipboard could not be read or written."""


_bound = False


def _bind() -> tuple[ctypes.WinDLL, ctypes.WinDLL]:
    """Return (user32, kernel32) with correct signatures.

    Explicit restypes matter on 64-bit Python: ctypes defaults every return
    value to a 32-bit int, which silently truncates HANDLE/pointer values.
    """
    if sys.platform != "win32":
        raise ClipboardError("Clipboard access is only supported on Windows.")
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    global _bound
    if not _bound:
        from ctypes import wintypes

        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalFree.restype = wintypes.HGLOBAL
        _bound = True
    return user32, kernel32


def _open_clipboard(user32: ctypes.WinDLL) -> None:
    # Another process may hold the clipboard lock for a moment (clipboard
    # managers, RDP). Retry briefly instead of failing the button click.
    for _ in range(_OPEN_RETRIES):
        if user32.OpenClipboard(None):
            return
        time.sleep(_OPEN_RETRY_DELAY_SECONDS)
    raise ClipboardError("The Windows clipboard is busy. Try again.")


def set_text(text: str) -> None:
    """Replace the OS clipboard contents with ``text`` (CF_UNICODETEXT)."""
    user32, kernel32 = _bind()
    data = text.encode("utf-16-le") + b"\x00\x00"
    _open_clipboard(user32)
    try:
        if not user32.EmptyClipboard():
            raise ClipboardError("Could not clear the Windows clipboard.")
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise ClipboardError("Could not allocate clipboard memory.")
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            raise ClipboardError("Could not lock clipboard memory.")
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(handle)
        # On success the system owns the handle; free it only on failure.
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            raise ClipboardError("Could not write to the Windows clipboard.")
    finally:
        user32.CloseClipboard()


def get_text() -> str:
    """Return the OS clipboard text, or ``""`` when empty/non-text (e.g. an image).

    Windows synthesizes CF_UNICODETEXT from other text formats (CF_TEXT, OEM),
    so plain-text pastes work regardless of which app produced them.
    """
    user32, kernel32 = _bind()
    _open_clipboard(user32)
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise ClipboardError("Could not read the Windows clipboard.")
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()
