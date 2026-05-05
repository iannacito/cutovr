"""Tiny helper used by the smoke tests to extract the per-session CSRF
token from the rendered HTML, so each test can submit forms without
needing to know the templating internals.

Usage:

    from _csrf_helper import csrf_post, get_csrf_token

    csrf_post(client, "/login", {"email": "...", "password": "..."})
"""

import re

# The hidden input we add to every form template.
_CSRF_RE = re.compile(
    r'<input[^>]+name="csrf_token"[^>]+value="([^"]+)"', re.IGNORECASE
)


def get_csrf_token(client, path="/login"):
    """GET `path` and pull the CSRF token out of the response.

    /login is the safest default because it's anonymous and always renders.
    """
    r = client.get(path)
    m = _CSRF_RE.search(r.data.decode("utf-8", "replace"))
    if not m:
        raise AssertionError(f"No csrf_token in response from {path}")
    return m.group(1)


def csrf_post(client, path, data, **kwargs):
    """POST with the current session's CSRF token added to the form data."""
    token = get_csrf_token(client)
    payload = dict(data)
    payload.setdefault("csrf_token", token)
    return client.post(path, data=payload, **kwargs)
