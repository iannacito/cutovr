"""importer.py — GL journal entry import background thread + polling.

Manages async posting of GL journal entries to QuickBooks, avoiding
platform request-timeout kills on large imports. Mirrors initialpost.py's
in-memory-state + background-thread pattern.

State is stored in _IMPORT_STATE keyed by job_id.
perform_import_fn receives job_id (re-hydrates from DB internally).
"""
from __future__ import annotations

import logging
import threading

_log = logging.getLogger(__name__)

# ── In-memory state ────────────────────────────────────────────────────────────
# Key: job_id (str)

_IMPORT_STATE: dict[str, dict] = {}
_IMPORT_LOCK = threading.Lock()


def _blank_state() -> dict:
    return {
        "overall": "running",
        "pushed": 0,
        "total": 0,
        "skipped": 0,
        "msg": "",
        "thread": None,
        "entries": {},
    }


def start_import(
    job_id: str,
    perform_import_fn,
    save_fn,
    real_import: bool,
) -> dict:
    """Start (or continue) GL import in background.

    Returns serializable state dict immediately (before thread finishes).
    perform_import_fn signature: (job_id, real_import, progress_fn) → None
    (updates job via save_fn, raises RuntimeError on fatal failure)
    """
    with _IMPORT_LOCK:
        existing = _IMPORT_STATE.get(job_id)
        if existing and existing.get("thread") and existing["thread"].is_alive():
            _log.warning("start_import: job %s already has a running import thread, returning existing state", job_id)
            return {"overall": existing["overall"], "pushed": existing["pushed"], "total": existing["total"]}

        state = _blank_state()
        _IMPORT_STATE[job_id] = state

    t = threading.Thread(
        target=_run_import_background,
        args=(job_id, perform_import_fn, save_fn, real_import),
        daemon=True,
    )
    with _IMPORT_LOCK:
        stored = _IMPORT_STATE.get(job_id)
        if stored is not None:
            stored["thread"] = t
    t.start()

    return {"overall": state["overall"], "pushed": state["pushed"], "total": state["total"]}


def update_entries(job_id: str, updates: dict) -> None:
    """Merge a batch of per-JE status updates into _IMPORT_STATE[job_id]['entries'].

    `updates` is {txn_id: {...fields..., status: 'processing'|'ok'|'rejected'}}.
    Rows marked 'ok' are dropped after one poll cycle (see get_import_state_json).
    """
    with _IMPORT_LOCK:
        state = _IMPORT_STATE.get(job_id)
        if state is None:
            return
        state.setdefault("entries", {})
        state["entries"].update(updates)


def get_import_state_json(job_id: str) -> dict | None:
    """Return serializable import state (no Thread object).

    Prunes 'ok' entries after reporting them once to keep payload size bounded.
    """
    with _IMPORT_LOCK:
        state = _IMPORT_STATE.get(job_id)
        if state is None:
            return None
        entries = state.get("entries", {})
        out_entries = list(entries.values())
        # Drop 'ok' rows after reporting them once — the frontend removes
        # them from view on receipt, no need to keep re-sending forever.
        for k, v in list(entries.items()):
            if v.get("status") == "ok":
                del entries[k]
        return {
            "overall": state["overall"],
            "pushed": state["pushed"],
            "total": state["total"],
            "skipped": state["skipped"],
            "msg": state["msg"],
            "entries": out_entries,
        }


# ── Background thread ──────────────────────────────────────────────────────────


def _run_import_background(
    job_id: str,
    perform_import_fn,
    save_fn,
    real_import: bool,
) -> None:
    """Run GL import in background. Updates _IMPORT_STATE and saves job."""

    def _make_progress(total_count: int):
        """Return a progress callback for the import loop."""
        def _cb(pushed: int) -> None:
            with _IMPORT_LOCK:
                state = _IMPORT_STATE.get(job_id)
                if state:
                    state["pushed"] = pushed
                    state["total"] = total_count
                    state["msg"] = f"{pushed}/{total_count} posted"
        return _cb

    try:
        with _IMPORT_LOCK:
            state = _IMPORT_STATE.get(job_id)
            if state:
                state["overall"] = "running"
                state["msg"] = "Importing..."

        perform_import_fn(job_id, real_import, _make_progress)

        with _IMPORT_LOCK:
            state = _IMPORT_STATE.get(job_id)
            if state:
                state["overall"] = "done"
                state["msg"] = f"Completed: {state['pushed']} posted, {state['skipped']} skipped"

    except Exception as exc:  # noqa: BLE001
        _log.exception("_run_import_background: import failed for job %s", job_id)
        with _IMPORT_LOCK:
            state = _IMPORT_STATE.get(job_id)
            if state:
                state["overall"] = "failed"
                state["msg"] = f"Import failed: {str(exc)[:100]}"
        # Persist to the job record so a full page reload can see the failure.
        # This is the only place that's guaranteed to run regardless of where
        # inside perform_import_fn the exception came from (validation gate,
        # unmapped accounts, token expiry, posting loop, etc.).
        try:
            save_fn(job_id, {
                "checkpoint": "needs_attention",
                "last_error": f"{type(exc).__name__}: {exc}"[:500],
            })
        except Exception:  # noqa: BLE001
            _log.exception(
                "_run_import_background: also failed to persist failure state for job %s",
                job_id,
            )
