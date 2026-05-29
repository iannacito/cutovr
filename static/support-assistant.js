// Floating support assistant. Calls the deterministic /support/assistant
// endpoint. Intentionally tiny and dependency-free — no live operator,
// no customer-data access, always provides a fallback.
(function () {
  "use strict";

  function ready(fn) {
    if (document.readyState !== "loading") {
      fn();
    } else {
      document.addEventListener("DOMContentLoaded", fn);
    }
  }

  ready(function () {
    var root = document.getElementById("support-assistant");
    if (!root) return;
    var toggle = root.querySelector(".support-assistant__toggle");
    var panel = root.querySelector(".support-assistant__panel");
    var closeBtn = root.querySelector(".support-assistant__close");
    var form = root.querySelector(".support-assistant__form");
    var input = root.querySelector("input[name='query']");
    var log = root.querySelector(".support-assistant__log");
    var topicsContainer = root.querySelector(".support-assistant__topics");
    var endpoint = root.getAttribute("data-endpoint");
    var csrf = root.getAttribute("data-csrf") || "";
    var supportEmail = root.getAttribute("data-support-email") || "";

    if (!toggle || !panel || !form || !input || !log) return;

    function setOpen(open) {
      if (open) {
        panel.hidden = false;
        root.setAttribute("data-state", "open");
        toggle.setAttribute("aria-expanded", "true");
        try { input.focus(); } catch (_) { /* ignore focus errors */ }
      } else {
        panel.hidden = true;
        root.setAttribute("data-state", "closed");
        toggle.setAttribute("aria-expanded", "false");
      }
    }

    toggle.addEventListener("click", function () {
      setOpen(panel.hidden);
    });

    if (closeBtn) {
      closeBtn.addEventListener("click", function () {
        setOpen(false);
        // Return focus to the launcher so the next Tab / Enter
        // reopens reliably for keyboard users.
        try { toggle.focus(); } catch (_) { /* ignore focus errors */ }
      });
    }

    // ESC minimizes when the panel is open.
    document.addEventListener("keydown", function (event) {
      if (event.key !== "Escape" && event.keyCode !== 27) return;
      if (panel.hidden) return;
      setOpen(false);
      try { toggle.focus(); } catch (_) { /* ignore focus errors */ }
    });

    function appendEntry(role, text) {
      var item = document.createElement("li");
      item.className = "support-assistant__entry support-assistant__entry--" + role;
      var bubble = document.createElement("div");
      bubble.className = "support-assistant__bubble";
      bubble.textContent = text;
      item.appendChild(bubble);
      log.appendChild(item);
      log.scrollTop = log.scrollHeight;
    }

    function ask(query) {
      var q = (query || "").trim();
      if (!q) return;
      appendEntry("user", q);
      input.value = "";
      var pending = document.createElement("li");
      pending.className = "support-assistant__entry support-assistant__entry--assistant support-assistant__entry--pending";
      var pendingBubble = document.createElement("div");
      pendingBubble.className = "support-assistant__bubble";
      pendingBubble.textContent = "Thinking…";
      pending.appendChild(pendingBubble);
      log.appendChild(pending);

      fetch(endpoint, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrf
        },
        body: JSON.stringify({ query: q })
      }).then(function (resp) {
        return resp.json().catch(function () { return null; });
      }).then(function (data) {
        if (pending.parentNode) pending.parentNode.removeChild(pending);
        if (data && data.answer) {
          appendEntry("assistant", data.answer);
        } else {
          appendEntry(
            "assistant",
            "Sorry — something went wrong. Please email " + supportEmail + "."
          );
        }
      }).catch(function () {
        if (pending.parentNode) pending.parentNode.removeChild(pending);
        appendEntry(
          "assistant",
          "Sorry — couldn't reach the assistant. Please email " + supportEmail + "."
        );
      });
    }

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      ask(input.value);
    });

    if (topicsContainer) {
      topicsContainer.addEventListener("click", function (event) {
        var target = event.target;
        if (!target || target.tagName !== "BUTTON") return;
        var query = target.getAttribute("data-query") || target.textContent;
        ask(query);
      });
    }
  });
})();
