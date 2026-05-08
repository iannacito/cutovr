"""Simple sliding-window rate limiter.

Backed by SQLite (the same `AppDB` instance the rest of the app uses) so
state survives process restarts on a single Render instance. For a multi-
instance deployment this would need to move to Redis or similar; that's
called out in the docs.

Usage:

    limiter = RateLimiter(db, max_events=5, window_seconds=300)
    allowed, retry_after = limiter.check_and_record("login:1.2.3.4")
    if not allowed:
        flash("Too many attempts...")

Each `check_and_record` call:
  1) purges events older than `window_seconds` for the bucket,
  2) counts what's left,
  3) if under `max_events`, records the new event and returns (True, 0),
  4) else returns (False, seconds-until-oldest-event-falls-out-of-window).

The bucket key is opaque to the limiter — it's typically of the form
"<route>:<ip>" or "<route>:<email-lowercased>". The caller decides what
to key on.
"""

from __future__ import annotations

import time
from typing import Tuple


class RateLimiter:
    def __init__(self, db, *, max_events: int, window_seconds: int):
        self.db = db
        self.max_events = int(max_events)
        self.window_seconds = int(window_seconds)

    def check_and_record(self, bucket_key: str) -> Tuple[bool, int]:
        """Return (allowed, retry_after_seconds).

        retry_after_seconds is 0 when allowed; otherwise an upper bound on
        how long the caller should wait before trying again.
        """
        now = time.time()
        cutoff = now - self.window_seconds

        # Best-effort cleanup so the table doesn't grow forever. We
        # purge events older than 1 day; current-window queries use
        # `cutoff` so old rows beyond that don't change correctness.
        try:
            self.db.purge_old_rate_limit_events(now - 24 * 3600)
        except Exception:  # noqa: BLE001
            # Cleanup must never break the limiter. Continue.
            pass

        count = self.db.count_rate_limit_events(bucket_key, cutoff)
        if count >= self.max_events:
            # We don't have the exact oldest timestamp without another
            # query; window_seconds is a safe upper bound for retry-after.
            return (False, self.window_seconds)

        self.db.record_rate_limit_event(bucket_key, now)
        return (True, 0)


def client_ip(request) -> str:
    """Best-effort client IP. Honors a single hop of X-Forwarded-For when
    present (Render terminates TLS in front of gunicorn and forwards the
    real IP in this header). Falls back to remote_addr.

    Rate limiting on a spoofable header is acceptable here because the
    limiter is a soft brute-force speed bump, not an auth boundary.
    """
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        # Take the first entry (the original client per RFC 7239 / common
        # reverse-proxy convention).
        return xff.split(",")[0].strip() or (request.remote_addr or "unknown")
    return request.remote_addr or "unknown"
