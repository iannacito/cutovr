"""initialpost.py — Pre-GL initialization sequence.

Manages the sequence that must complete before GL journal entries are posted
to QuickBooks:

    Vendors   → auto-push sequential (background thread)
    Customers → auto-push sequential (background thread, after Vendors)
    GL        → JS enables Send button when above are done/skipped

State is stored in _SEQ_STATE keyed by (firm_id, gl_job_id).
push_fn and save_fn are injected from app.py (no circular imports).
progress_fn(pushed, total) is called after each entity push so the poll
endpoint can return live count/total in the step msg.
"""
from __future__ import annotations

import logging
import threading
import time as _time

_log = logging.getLogger(__name__)

# ── Sequence definition ───────────────────────────────────────────────────────

SEQUENCE: list[dict] = [
    {"key": "vendors",   "label": "Vendors",        "report_type": "vendor_list",   "mode": "auto"},
    {"key": "customers", "label": "Customers",       "report_type": "customer_list", "mode": "auto"},
    {"key": "gl",        "label": "General Ledger",  "report_type": "general_ledger","mode": "auto"},
]

AUTO_KEYS = [s["key"] for s in SEQUENCE if s["mode"] == "auto" and s["key"] != "gl"]

# ── In-memory state ───────────────────────────────────────────────────────────
# Key: (firm_id: int, gl_job_id: str)

_SEQ_STATE: dict[tuple, dict] = {}
_SEQ_LOCK  = threading.Lock()
_LABELS    = {s["key"]: s["label"] for s in SEQUENCE}


def _blank(key: str) -> dict:
    return {"key": key, "label": _LABELS[key], "status": "pending", "pct": 0, "msg": "", "url": None}


# ── Status helpers ────────────────────────────────────────────────────────────

def _find_job(jobs: list[dict], report_type: str) -> dict | None:
    matches = [j for j in jobs if j.get("report_type") == report_type]
    if not matches:
        return None
    matches.sort(key=lambda j: j.get("updated_at") or "", reverse=True)
    return matches[0]


def _url_for_job(jobs: list[dict], report_type: str, route_path: str) -> str | None:
    job = _find_job(jobs, report_type)
    return f"/jobs/{job['id']}/{route_path}" if job else None


def _start_auto_thread_if_needed(
    firm_id: int,
    gl_job_id: str,
    state: dict,
    jobs: list[dict],
    push_fn,
    save_fn,
    real_import: bool,
) -> None:
    """Start vendor/customer background thread when auto steps remain."""
    if state["overall"] in ("needs_action", "ready_for_gl", "done", "failed"):
        return
    t = threading.Thread(
        target=_run_auto_steps,
        args=(firm_id, gl_job_id, jobs, push_fn, save_fn, real_import),
        daemon=True,
    )
    with _SEQ_LOCK:
        stored = _SEQ_STATE.get((firm_id, gl_job_id))
        if stored is not None:
            stored["thread"] = t
    t.start()


# ── State builder ─────────────────────────────────────────────────────────────

def build_initial_state(firm_id: int, gl_job_id: str, jobs: list[dict]) -> dict:
    """Build state from current job data. Always reflects latest DB."""
    steps: dict[str, dict] = {s["key"]: _blank(s["key"]) for s in SEQUENCE}

    # Vendors
    v = _find_job(jobs, "vendor_list")
    if v and v.get("checkpoint") == "completed":
        steps["vendors"] = {"key": "vendors", "label": _LABELS["vendors"], "status": "completed", "pct": 100,
                            "msg": "Vendors pushed", "url": None}
    elif not v:
        steps["vendors"] = {"key": "vendors", "label": _LABELS["vendors"], "status": "skipped", "pct": 100,
                            "msg": "No Vendor List — skipped", "url": None}

    # Customers
    c = _find_job(jobs, "customer_list")
    if c and c.get("checkpoint") == "completed":
        steps["customers"] = {"key": "customers", "label": _LABELS["customers"], "status": "completed", "pct": 100,
                              "msg": "Customers pushed", "url": None}
    elif not c:
        steps["customers"] = {"key": "customers", "label": _LABELS["customers"], "status": "skipped", "pct": 100,
                              "msg": "No Customer List — skipped", "url": None}

    # GL always pending (JS triggers the actual submit)
    steps["gl"] = {"key": "gl", "label": _LABELS["gl"], "status": "pending", "pct": 0,
                   "msg": "Sends after above steps complete", "url": None}

    return {
        "steps": steps,
        "overall": _compute_overall(steps),
        "thread": None,
    }


def _rebuild_state(
    firm_id: int,
    gl_job_id: str,
    jobs: list[dict],
    existing: dict | None,
) -> dict:
    """Rebuild from DB."""
    return build_initial_state(firm_id, gl_job_id, jobs)


def _compute_overall(steps: dict) -> str:
    """Compute overall sequence state.

    COA and OB (ROUTE_KEYS) are informational — their status shows in the
    UI as warnings with action links, but they do NOT gate the GL post.
    Only Vendors and Customers (AUTO_KEYS) gate readiness.
    """
    for key in AUTO_KEYS:
        if steps[key]["status"] in ("in_progress", "initializing"):
            return "running"
        if steps[key]["status"] == "failed":
            return "failed"
    v_ok = steps["vendors"]["status"] in ("completed", "skipped")
    c_ok = steps["customers"]["status"] in ("completed", "skipped")
    if v_ok and c_ok and steps["gl"]["status"] == "pending":
        return "ready_for_gl"
    if steps["gl"]["status"] == "completed":
        return "done"
    return "running"


# ── Public API ────────────────────────────────────────────────────────────────

def get_state_json(firm_id: int, gl_job_id: str) -> dict | None:
    """Return serialisable state dict (no Thread object)."""
    with _SEQ_LOCK:
        state = _SEQ_STATE.get((firm_id, gl_job_id))
        if state is None:
            return None
        return {"overall": state["overall"], "steps": list(state["steps"].values())}


def start_sequence(
    firm_id: int,
    gl_job_id: str,
    jobs: list[dict],
    push_fn,
    save_fn,
    real_import: bool = False,
) -> dict:
    """Refresh state from DB and start background thread if auto steps remain.

    Returns serialisable state dict immediately (before thread finishes).
    push_fn signature: (job_dict, report_type, real_import, progress_fn) → (pushed, skipped, errors)
    """
    with _SEQ_LOCK:
        existing = _SEQ_STATE.get((firm_id, gl_job_id))
        if existing and existing.get("thread") and existing["thread"].is_alive():
            return {"overall": existing["overall"], "steps": list(existing["steps"].values())}

        state = _rebuild_state(firm_id, gl_job_id, jobs, existing)
        _SEQ_STATE[(firm_id, gl_job_id)] = state

    _start_auto_thread_if_needed(firm_id, gl_job_id, state, jobs, push_fn, save_fn, real_import)
    return {"overall": state["overall"], "steps": list(state["steps"].values())}


def retry_step(
    firm_id: int,
    gl_job_id: str,
    step_key: str,
    jobs: list[dict],
    push_fn,
    save_fn,
    real_import: bool = False,
) -> dict:
    """Reset a failed step and restart the background thread from it."""
    with _SEQ_LOCK:
        state = _SEQ_STATE.get((firm_id, gl_job_id))
        if not state:
            return start_sequence(firm_id, gl_job_id, jobs, push_fn, save_fn, real_import)
        if state.get("thread") and state["thread"].is_alive():
            return {"overall": state["overall"], "steps": list(state["steps"].values())}
        state["steps"][step_key] = _blank(step_key)
        state["overall"] = "running"

    t = threading.Thread(
        target=_run_auto_steps,
        args=(firm_id, gl_job_id, jobs, push_fn, save_fn, real_import),
        daemon=True,
    )
    with _SEQ_LOCK:
        state = _SEQ_STATE[(firm_id, gl_job_id)]
        state["thread"] = t
    t.start()

    with _SEQ_LOCK:
        state = _SEQ_STATE[(firm_id, gl_job_id)]
        return {"overall": state["overall"], "steps": list(state["steps"].values())}


# ── Background thread ─────────────────────────────────────────────────────────

def _run_auto_steps(
    firm_id:     int,
    gl_job_id:   str,
    jobs:        list[dict],
    push_fn,
    save_fn,
    real_import: bool,
) -> None:
    """Sequential: Vendors then Customers. Updates _SEQ_STATE in-place."""
    for key in AUTO_KEYS:  # ["vendors", "customers"]
        with _SEQ_LOCK:
            state = _SEQ_STATE.get((firm_id, gl_job_id))
            if not state:
                return
            step_status = state["steps"][key]["status"]

        if step_status in ("completed", "skipped"):
            continue

        rtype = next(s["report_type"] for s in SEQUENCE if s["key"] == key)
        job   = _find_job(jobs, rtype)
        if not job:
            _update(firm_id, gl_job_id, key, "skipped", 100, "No file — skipped")
            continue

        _update(firm_id, gl_job_id, key, "in_progress", 5, "Connecting to QuickBooks…")

        # Build a progress callback that writes live count/total into msg
        def _make_progress(fid, gid, k):
            def _cb(pushed: int, total: int) -> None:
                pct = int(pushed / total * 100) if total else 50
                _update(fid, gid, k, "in_progress", max(pct, 5), f"{pushed}/{total} pushed")
            return _cb

        try:
            pushed, skipped, errors = push_fn(
                job, rtype, real_import,
                progress_fn=_make_progress(firm_id, gl_job_id, key),
            )
            save_fn(job["id"], {"status": "completed", "checkpoint": "completed"})
            if pushed:
                msg = f"{pushed} synced"
            elif errors:
                msg = f"{errors} failed"
            else:
                msg = "Already in QBO"
            if errors and pushed:
                msg += f", {errors} failed"
            if skipped:
                msg += f", {skipped} skipped"
            _update(firm_id, gl_job_id, key, "completed", 100, msg)
        except Exception as exc:  # noqa: BLE001
            _log.error("initialpost thread %s failed: %s", key, exc)
            _update(firm_id, gl_job_id, key, "failed", 0, str(exc)[:120])
            _set_overall(firm_id, gl_job_id, "failed")
            return

        _time.sleep(0.3)

    with _SEQ_LOCK:
        state = _SEQ_STATE.get((firm_id, gl_job_id))
        if state:
            state["overall"] = _compute_overall(state["steps"])


def _update(firm_id, gl_job_id, key, status, pct, msg):
    with _SEQ_LOCK:
        state = _SEQ_STATE.get((firm_id, gl_job_id))
        if state:
            state["steps"][key].update(status=status, pct=pct, msg=msg)
            state["overall"] = _compute_overall(state["steps"])


def _set_overall(firm_id, gl_job_id, overall):
    with _SEQ_LOCK:
        state = _SEQ_STATE.get((firm_id, gl_job_id))
        if state:
            state["overall"] = overall
