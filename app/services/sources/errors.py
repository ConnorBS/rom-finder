"""Typed errors for ROM sources.

Sources used to swallow failures (`except Exception: return []`), so a 403
token rejection, a 429 rate-limit, a dead mirror, and a genuine "no results"
all looked identical — and a network failure during verify looked like
"not in RA database". Raising these typed errors lets the routers surface the
real cause (in the results partial + /logs) and lets callers back off correctly.

Plain exceptions, no framework. `user_message` (str(exc)) is safe to show in UI.
"""

from typing import Optional


class SourceError(Exception):
    """Base for ROM-source failures."""
    def __init__(self, message: str, *, source_id: str = "", retry_after: Optional[float] = None):
        super().__init__(message)
        self.source_id = source_id
        self.retry_after = retry_after


class SourceNetworkError(SourceError):
    """Timeout, connection failure, 5xx, or a malformed/short response body."""


class SourceForbiddenError(SourceError):
    """HTTP 403 — bot block or a rejected/expired signed token (e.g. ROMsFun CDN)."""


class SourceRateLimitError(SourceError):
    """HTTP 429 — carries retry_after seconds when the server provided it."""


class SourceNotFoundError(SourceError):
    """HTTP 404 — the item/file isn't there."""


class SourceBadHashError(SourceError):
    """Downloaded fine, but the file's hash didn't match what was expected."""


def classify_status(status: int, *, source_id: str = "", retry_after: Optional[float] = None,
                    detail: str = "") -> SourceError:
    """Map an HTTP status to the right typed error for `raise`."""
    msg = detail or f"HTTP {status}"
    if status == 403:
        return SourceForbiddenError(msg, source_id=source_id)
    if status == 404:
        return SourceNotFoundError(msg, source_id=source_id)
    if status == 429:
        return SourceRateLimitError(msg, source_id=source_id, retry_after=retry_after)
    return SourceNetworkError(msg, source_id=source_id)
