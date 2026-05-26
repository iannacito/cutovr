/* Password show/hide toggle.
 *
 * Wires up any element with [data-password-toggle] to flip the
 * `type` attribute of the password <input> it sits next to (or the
 * input named by data-password-toggle-target). Buttons use
 * type="button" so they never submit the surrounding form.
 *
 * The label / aria-label / aria-pressed state update together so
 * screen readers announce the change.
 */
(function () {
  function findTarget(btn) {
    var name = btn.getAttribute("data-password-toggle-target");
    if (name) {
      var form = btn.closest("form");
      var scope = form || document;
      return scope.querySelector('input[name="' + name + '"]');
    }
    // Default: nearest sibling password input inside the same label/wrapper.
    var wrapper = btn.closest("label, .pwd-field");
    if (wrapper) {
      return wrapper.querySelector('input[type="password"], input[data-was-password]');
    }
    return null;
  }

  function apply(btn) {
    var input = findTarget(btn);
    if (!input) return;
    var showing = input.getAttribute("type") === "text";
    if (showing) {
      input.setAttribute("type", "password");
      btn.setAttribute("aria-pressed", "false");
      btn.setAttribute("aria-label", "Show password");
      var showText = btn.querySelector(".pwd-toggle-text");
      if (showText) showText.textContent = "Show";
    } else {
      input.setAttribute("type", "text");
      input.setAttribute("data-was-password", "1");
      btn.setAttribute("aria-pressed", "true");
      btn.setAttribute("aria-label", "Hide password");
      var hideText = btn.querySelector(".pwd-toggle-text");
      if (hideText) hideText.textContent = "Hide";
    }
  }

  function init() {
    var buttons = document.querySelectorAll("[data-password-toggle]");
    for (var i = 0; i < buttons.length; i++) {
      var btn = buttons[i];
      // Defensive: ensure the control never submits a form.
      if (!btn.hasAttribute("type")) btn.setAttribute("type", "button");
      btn.addEventListener("click", (function (b) {
        return function (e) {
          e.preventDefault();
          apply(b);
        };
      })(btn));
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
