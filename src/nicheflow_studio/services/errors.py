"""Shared exception base for UI-independent services.

Service-layer errors that carry a user-facing message subclass
:class:`ServiceError`. The pywebview bridge catches this base and returns the
message verbatim in its error envelope, while truly unexpected exceptions are
logged and reported generically. The Codex CLI catches the specific subclasses
it cares about.
"""

from __future__ import annotations


class ServiceError(Exception):
    """Base for service errors whose message is safe to show the user."""
