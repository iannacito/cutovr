"""Canonical, resumable migration checkpoints.

The customer-facing app shows a free-text ``job["status"]`` string (plain
English prose like "Imported 12 journal entries to QuickBooks"). That is
great for humans but useless for *resuming* a job after a refresh, a
re-login, or a retry — the prose drifts and is not a stable machine value.

This module defines a small, stable vocabulary of checkpoints and the
ordering between them, plus a helper to map a checkpoint back to the
workflow step the customer should land on next. It is deliberately tiny:
the durable foundation for resume/operator-summary work, not a task queue.

Stages (in order):

  uploaded        file accepted, not yet parsed/validated
  parsed          GL parsed + preflight computed
  matched         accounts mapped to QuickBooks
  reviewed        customer reviewed the preview
  importing       a QuickBooks write is in progress
  completed       journal entries posted + verified
  needs_attention something blocks progress (validation, unmapped, error)

``needs_attention`` is intentionally *not* in the linear order — it is a
side state any stage can fall into. ``resume_step`` routes it back to the
earliest place the customer can act.
"""

from __future__ import annotations

UPLOADED = "uploaded"
PARSED = "parsed"
MATCHED = "matched"
REVIEWED = "reviewed"
IMPORTING = "importing"
COMPLETED = "completed"
NEEDS_ATTENTION = "needs_attention"

# Linear progression. needs_attention is handled separately.
ORDER = [UPLOADED, PARSED, MATCHED, REVIEWED, IMPORTING, COMPLETED]

ALL = ORDER + [NEEDS_ATTENTION]

# Map each checkpoint to the workflow step number (1-6) the customer should
# resume at. Keeps routing out of the view layer so a refresh/login lands
# the customer on the right step without guessing from prose.
RESUME_STEP = {
    UPLOADED: 2,
    PARSED: 3,
    MATCHED: 4,
    REVIEWED: 5,
    IMPORTING: 5,
    COMPLETED: 6,
    NEEDS_ATTENTION: 4,
}


def is_valid(checkpoint) -> bool:
    return checkpoint in ALL


def rank(checkpoint) -> int:
    """Position in the linear order. needs_attention and unknown sort low
    so a real forward stage always wins a max() comparison."""
    try:
        return ORDER.index(checkpoint)
    except ValueError:
        return -1


def advance(current, target) -> str:
    """Return the checkpoint a job should hold after reaching ``target``.

    Never moves a job *backwards* along the linear order — recording
    ``parsed`` on an already-``completed`` job keeps it ``completed``.
    But any stage can drop into ``needs_attention`` (a blocker is a
    blocker), and a job in ``needs_attention`` can recover to any forward
    stage once the blocker clears.
    """
    if target == NEEDS_ATTENTION:
        return NEEDS_ATTENTION
    if current == NEEDS_ATTENTION:
        return target
    if not is_valid(current):
        return target
    return target if rank(target) >= rank(current) else current


def resume_step(checkpoint) -> int:
    """Workflow step (1-6) to resume at for this checkpoint."""
    return RESUME_STEP.get(checkpoint, 1)
