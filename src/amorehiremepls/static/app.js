// Progressive enhancement only: every control works as a plain form without this file.
(function () {
  "use strict";
  var root = document.documentElement;

  function resolvedTheme() {
    var t = root.getAttribute("data-theme");
    if (t === "light" || t === "dark") return t;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  // Header theme toggle: flip immediately, persist in the background through the CSRF-protected form endpoint.
  document.querySelectorAll("form.theme-form").forEach(function (form) {
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var next = resolvedTheme() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      form.querySelector('input[name="theme"]').value = next;
      var body = new URLSearchParams(new FormData(form));
      fetch(form.action, {
        method: "POST",
        body: body,
        credentials: "same-origin",
        headers: { "X-Requested-With": "fetch", "Accept": "application/json" },
      }).catch(function () { /* the next full page load re-syncs from the saved setting */ });
    });
  });

  // Settings: live accent preview from presets or the free hex field.
  var hexInput = document.querySelector('input[name="accent_hex"]');
  function applyAccent(v) {
    if (/^#[0-9a-fA-F]{6}$/.test(v)) root.style.setProperty("--accent", v);
  }
  document.querySelectorAll('input[name="accent"]').forEach(function (r) {
    r.addEventListener("change", function () {
      if (r.value !== "custom") { applyAccent(r.dataset.hex || ""); if (hexInput) hexInput.value = r.dataset.hex || hexInput.value; }
      else if (hexInput) applyAccent(hexInput.value);
    });
  });
  if (hexInput) {
    hexInput.addEventListener("input", function () {
      applyAccent(hexInput.value);
      var custom = document.querySelector('input[name="accent"][value="custom"]');
      if (custom) custom.checked = true;
    });
    var picker = document.querySelector('input[name="accent_picker"]');
    if (picker) {
      picker.addEventListener("input", function () { hexInput.value = picker.value; hexInput.dispatchEvent(new Event("input")); });
    }
  }
  document.querySelectorAll('input[name="theme"][type="radio"]').forEach(function (r) {
    r.addEventListener("change", function () {
      if (r.value === "system") root.removeAttribute("data-theme"); else root.setAttribute("data-theme", r.value);
    });
  });

  // Close popovers (emergency stop, account menu) when clicking elsewhere or pressing Escape.
  function closePops(except) {
    document.querySelectorAll("details.pop[open]").forEach(function (d) { if (d !== except) d.removeAttribute("open"); });
  }
  document.addEventListener("click", function (ev) {
    var inside = ev.target.closest && ev.target.closest("details.pop");
    closePops(inside);
  });
  document.addEventListener("keydown", function (ev) { if (ev.key === "Escape") closePops(null); });

  // Double-submit guard for state-changing forms.
  document.querySelectorAll("form[method='post']").forEach(function (form) {
    form.addEventListener("submit", function () {
      if (form.classList.contains("theme-form")) return;
      var btn = form.querySelector("button[type='submit'], button:not([type])");
      if (btn) setTimeout(function () { btn.disabled = true; }, 0);
    });
  });
})();
