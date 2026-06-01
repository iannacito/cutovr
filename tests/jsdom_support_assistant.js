/* End-to-end functional test for the floating support assistant widget.
 *
 * Loads the real static CSS, injects the real static JS, and exercises
 * the Minimize -> reopen -> ESC round-trip in jsdom. This is the test
 * that would have caught the bug where clicking Minimize updated the
 * hidden attribute + data-state but the CSS `display: flex` rule on
 * the panel kept it visible on screen.
 *
 * Invoked by tests/smoke_support_assistant.py (T13) which provides:
 *   - NODE_PATH pointing at a node_modules dir containing jsdom
 *   - SUPPORT_ASSISTANT_REPO_ROOT pointing at the project root
 *
 * Exit codes: 0 = pass, non-zero = fail with diagnostic on stderr.
 */

"use strict";

const fs = require("fs");
const path = require("path");

const ROOT = process.env.SUPPORT_ASSISTANT_REPO_ROOT;
if (!ROOT) {
  console.error("SUPPORT_ASSISTANT_REPO_ROOT must be set");
  process.exit(2);
}

let JSDOM;
try {
  ({ JSDOM } = require("jsdom"));
} catch (err) {
  console.error("jsdom not installed (NODE_PATH=" + process.env.NODE_PATH + ")");
  console.error(err.message);
  process.exit(2);
}

const css = fs.readFileSync(path.join(ROOT, "static", "style.css"), "utf8");
const js = fs.readFileSync(path.join(ROOT, "static", "support-assistant.js"), "utf8");

// Mirrors templates/_base.html with Jinja substituted. Kept in sync by
// hand; the rule-based guards in smoke_support_assistant.py catch
// drift in the production template structure.
const html = `<!doctype html><html><head><style>${css}</style></head><body>
<div id="support-assistant"
     class="support-assistant"
     data-testid="support-assistant"
     data-state="closed"
     data-endpoint="/support/assistant"
     data-csrf=""
     data-support-email="support@example.com">
  <button type="button"
          class="support-assistant__toggle"
          data-testid="support-assistant-toggle"
          aria-expanded="false"
          aria-controls="support-assistant-panel"
          aria-label="Open support assistant">
    <span aria-hidden="true">?</span>
    <span>Need help?</span>
  </button>
  <section id="support-assistant-panel"
           class="support-assistant__panel"
           role="dialog"
           aria-label="Ask Cutovr"
           hidden>
    <header class="support-assistant__header">
      <strong>Ask Cutovr</strong>
      <button type="button"
              class="support-assistant__close"
              data-testid="support-assistant-close"
              aria-label="Minimize support assistant"
              title="Minimize">
        <span aria-hidden="true">&minus;</span>
        <span class="support-assistant__close-label">Minimize</span>
      </button>
    </header>
    <div class="support-assistant__body" data-testid="support-assistant-body">
      <p class="support-assistant__intro">Intro.</p>
      <div class="support-assistant__topics" data-testid="support-assistant-topics">
        <button type="button" class="support-assistant__topic" data-query="what is cutovr">What does Cutovr do?</button>
      </div>
      <ol class="support-assistant__log" data-testid="support-assistant-log" aria-live="polite"></ol>
    </div>
    <form class="support-assistant__form" data-testid="support-assistant-form">
      <label class="visually-hidden" for="support-assistant-input">Your question</label>
      <input type="text" id="support-assistant-input" name="query" placeholder="Type your question…" autocomplete="off" maxlength="500" required>
      <button type="submit" class="btn btn-primary btn-sm">Ask</button>
    </form>
  </section>
</div>
</body></html>`;

const dom = new JSDOM(html, { runScripts: "outside-only", pretendToBeVisual: true });
const { window } = dom;
window.eval(js);
window.document.dispatchEvent(new window.Event("DOMContentLoaded"));

const root = window.document.getElementById("support-assistant");
const toggle = root.querySelector(".support-assistant__toggle");
const panel = root.querySelector(".support-assistant__panel");
const closeBtn = root.querySelector(".support-assistant__close");

function panelDisplay() {
  return window.getComputedStyle(panel).display;
}
function toggleDisplay() {
  return window.getComputedStyle(toggle).display;
}

function assertEq(actual, expected, label) {
  if (actual !== expected) {
    console.error(
      `ASSERTION FAILED at ${label}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`
    );
    console.error(
      "  data-state=" + root.getAttribute("data-state") +
      " panel.hidden=" + panel.hidden +
      " panel.display=" + panelDisplay() +
      " toggle.display=" + toggleDisplay()
    );
    process.exit(1);
  }
}

// 1) Initial state: panel must be visually hidden, launcher visible.
assertEq(root.getAttribute("data-state"), "closed", "initial data-state");
assertEq(panel.hidden, true, "initial panel.hidden");
assertEq(panelDisplay(), "none", "initial panel computed display");

// 2) Click launcher pill -> panel becomes visible.
toggle.click();
assertEq(root.getAttribute("data-state"), "open", "after toggle: data-state");
assertEq(panel.hidden, false, "after toggle: panel.hidden");
if (panelDisplay() === "none") {
  console.error("after toggle: panel is still display:none — open failed");
  process.exit(1);
}

// 3) Click Minimize -> panel must visually disappear (computed display:none).
//    This is the assertion that would have failed pre-fix.
closeBtn.click();
assertEq(root.getAttribute("data-state"), "closed", "after minimize: data-state");
assertEq(panel.hidden, true, "after minimize: panel.hidden");
assertEq(
  panelDisplay(),
  "none",
  "after minimize: panel computed display (this is the bug fix's load-bearing assertion)"
);
// Launcher pill must reappear so the user can reopen.
if (toggleDisplay() === "none") {
  console.error("after minimize: launcher pill is still hidden — user can't reopen");
  process.exit(1);
}

// 4) Click launcher again -> reopens reliably.
toggle.click();
assertEq(root.getAttribute("data-state"), "open", "reopen: data-state");
assertEq(panel.hidden, false, "reopen: panel.hidden");
if (panelDisplay() === "none") {
  console.error("reopen: panel did not become visible");
  process.exit(1);
}

// 5) ESC closes when panel is open.
window.document.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", keyCode: 27 }));
assertEq(root.getAttribute("data-state"), "closed", "ESC: data-state");
assertEq(panel.hidden, true, "ESC: panel.hidden");
assertEq(panelDisplay(), "none", "ESC: panel computed display");

// 6) ESC while closed is a no-op (must not flip state back open).
window.document.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", keyCode: 27 }));
assertEq(root.getAttribute("data-state"), "closed", "ESC while closed stays closed");
assertEq(panel.hidden, true, "ESC while closed keeps panel hidden");

console.log("jsdom round-trip OK: minimize -> reopen -> ESC all produce correct computed display");
