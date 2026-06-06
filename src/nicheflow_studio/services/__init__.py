"""UI-independent application services.

Modules here hold plain-Python business logic that is shared by the existing
PyQt UI, the Codex repository CLI, and the future pywebview/React bridge. They
must not import UI toolkits, so any caller (CLI, bridge, tests) can use them.
"""
