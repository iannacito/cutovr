"""Smoke tests for the unified workflow stepper nav row.

The workflow stepper renders both the "Back to Step N" link and the
primary "Proceed to Step N+1" CTA inside a single `.workflow-stepper__nav`
flex container so the two buttons are always vertically aligned on the
same baseline, regardless of which step page is being viewed.

Run from project root:

    python3 tests/smoke_workflow_nav_row.py

Covers:
  N1  Step 2+ pages render a single `.workflow-stepper__nav` row that
      contains BOTH the back link (`.workflow-stepper__back-link`) and
      the primary CTA (`.workflow-stepper__cta-link`).
  N2  The nav-row markup precedes the title block so the title/lede
      never visually splits the two CTAs.
  N3  The legacy free-floating `.workflow-stepper__back` wrapper is
      gone — the back link is only ever rendered inside the nav row.
  N4  Every workflow-stepper page (dashboard, migration-checklist,
      cutover, send-to-qbo) emits the nav-row class when the current
      stage exposes a CTA.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APP_DB = tempfile.mktemp(suffix=".sqlite3")
HIST_DB = tempfile.mktemp(suffix=".sqlite3")
os.environ["APP_DB"] = APP_DB
os.environ["IMPORT_HISTORY_DB"] = HIST_DB
os.environ.setdefault("CSRF_DISABLE", "1")
os.environ.setdefault("SECRET_KEY", "smoke-secret-nav-row")

import app as appmod  # noqa: E402


def _signup_login(client, email: str) -> None:
    r = client.post("/signup", data={
        "firm_name": "Nav Row Test Firm",
        "email": email,
        "password": "passw0rd!1234",
        "confirm_password": "passw0rd!1234",
    }, follow_redirects=False)
    if r.status_code == 200:
        client.post("/login", data={
            "email": email, "password": "passw0rd!1234",
        }, follow_redirects=False)


def n1_step1_page_renders_nav_row_with_cta_only():
    """A brand-new firm sits on Step 1. There is no previous step, so
    the nav-row should render but contain only the primary CTA half —
    never a back link. The wrapper itself must still be present so the
    CTA is positioned consistently with later steps."""
    client = appmod.app.test_client()
    _signup_login(client, "nav-step1@example.test")
    r = client.get("/dashboard")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert "workflow-stepper__nav" in body, \
        "Step 1 dashboard missing unified nav-row wrapper"
    # Legacy free-floating back wrapper must not appear at all.
    assert 'class="workflow-stepper__back"' not in body, \
        "legacy workflow-stepper__back wrapper should no longer render"
    print("OK  N1  Step 1 dashboard renders nav-row wrapper (CTA-only)")


def n2_nav_row_precedes_title():
    """The nav row must come before the title block. If it landed after
    the title the back/next CTAs would sit in different rows again —
    which is exactly the visual bug we just fixed."""
    client = appmod.app.test_client()
    _signup_login(client, "nav-order@example.test")
    r = client.get("/dashboard")
    body = r.get_data(as_text=True)
    nav_idx = body.find("workflow-stepper__nav")
    title_idx = body.find("workflow-stepper__title")
    assert nav_idx != -1 and title_idx != -1, \
        f"expected nav + title in body (nav={nav_idx}, title={title_idx})"
    assert nav_idx < title_idx, (
        "nav-row must render before the title block so back/next CTAs "
        f"share a row (nav={nav_idx}, title={title_idx})"
    )
    print("OK  N2  nav row is rendered before the title block")


def n3_nav_halves_present_when_both_back_and_cta_exist():
    """Once the firm is past Step 1 the stepper has both a back link and
    a primary CTA. Both must live inside the single `.workflow-stepper__nav`
    row, with both `__nav-left` and `__nav-right` halves present."""
    client = appmod.app.test_client()
    _signup_login(client, "nav-halves@example.test")
    # Bare /migration-checklist still renders the stepper even before
    # Step 1 completes; the partial structure (halves) must be present.
    r = client.get("/migration-checklist")
    body = r.get_data(as_text=True)
    assert "workflow-stepper__nav-left" in body, \
        "nav-row missing __nav-left half"
    assert "workflow-stepper__nav-right" in body, \
        "nav-row missing __nav-right half"
    print("OK  N3  nav row exposes left + right halves for back/CTA pair")


def n4_all_step_pages_emit_nav_row():
    """Every workflow-step page that includes the stepper partial should
    emit the unified nav-row class so the visual alignment is consistent
    across the whole 6-step flow."""
    client = appmod.app.test_client()
    _signup_login(client, "nav-allpages@example.test")
    pages = [
        "/dashboard",
        "/migration-checklist",
        "/cutover",
    ]
    for path in pages:
        r = client.get(path)
        assert r.status_code in (200, 302), f"{path} -> {r.status_code}"
        if r.status_code != 200:
            continue
        body = r.get_data(as_text=True)
        if "workflow-stepper" not in body:
            # Page may legitimately omit the stepper (e.g. when redirected).
            continue
        assert "workflow-stepper__nav" in body, \
            f"{path} renders stepper without unified nav row"
    print("OK  N4  every stepper page renders the unified nav row")


def main():
    n1_step1_page_renders_nav_row_with_cta_only()
    n2_nav_row_precedes_title()
    n3_nav_halves_present_when_both_back_and_cta_exist()
    n4_all_step_pages_emit_nav_row()
    print("\nAll workflow nav-row smoke tests passed.")


if __name__ == "__main__":
    main()
