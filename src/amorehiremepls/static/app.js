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

  // Company logos: when an app-served logo fails to load (no icon for that employer), show the initials again.
  function logoFailed(img) {
    var holder = img.closest(".avatar");
    if (holder) holder.classList.remove("has-logo");
  }
  document.querySelectorAll(".avatar img.logo").forEach(function (img) {
    img.addEventListener("error", function () { logoFailed(img); });
    if (img.complete && img.naturalWidth === 0) logoFailed(img);
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

  // Settings: live background-tint preview. "accent" means follow --accent (the stylesheet default).
  var tintHex = document.querySelector('input[name="tint_hex"]');
  function applyTint(v) {
    if (v === "accent" || v === "") root.style.removeProperty("--tint");
    else if (/^#[0-9a-fA-F]{6}$/.test(v)) root.style.setProperty("--tint", v);
  }
  document.querySelectorAll('input[name="tint"]').forEach(function (r) {
    r.addEventListener("change", function () {
      if (r.value === "custom") { if (tintHex) applyTint(tintHex.value); return; }
      applyTint(r.dataset.hex || "accent");
      if (tintHex && r.dataset.hex) tintHex.value = r.dataset.hex;
    });
  });
  if (tintHex) {
    tintHex.addEventListener("input", function () {
      applyTint(tintHex.value);
      var custom = document.querySelector('input[name="tint"][value="custom"]');
      if (custom) custom.checked = true;
    });
    var tintPicker = document.querySelector('input[name="tint_picker"]');
    if (tintPicker) {
      tintPicker.addEventListener("input", function () { tintHex.value = tintPicker.value; tintHex.dispatchEvent(new Event("input")); });
    }
  }

  // Overview: the Getting started card folds from its header. The remembered state lives in localStorage and was
  // already applied to <html data-checklist> by the inline script before first paint; here we only sync the button
  // and handle clicks (Enter/Space come for free on a real <button>).
  var checklistToggle = document.querySelector(".checklist-toggle");
  if (checklistToggle) {
    var KEY = "amore_checklist";
    function setFolded(folded, remember) {
      if (folded) root.setAttribute("data-checklist", "folded"); else root.removeAttribute("data-checklist");
      checklistToggle.setAttribute("aria-expanded", folded ? "false" : "true");
      if (remember) { try { localStorage.setItem(KEY, folded ? "folded" : "open"); } catch (e) { /* private mode: not remembered */ } }
    }
    setFolded(root.getAttribute("data-checklist") === "folded", false);
    checklistToggle.addEventListener("click", function () {
      setFolded(checklistToggle.getAttribute("aria-expanded") !== "false", true);
    });
  }

  // Close popovers (emergency stop, account menu, More actions) when clicking elsewhere or pressing Escape.
  function closePops(except) {
    document.querySelectorAll("details.pop[open]").forEach(function (d) { if (d !== except) d.removeAttribute("open"); });
  }
  document.addEventListener("click", function (ev) {
    var inside = ev.target.closest && ev.target.closest("details.pop");
    closePops(inside);
  });
  document.addEventListener("keydown", function (ev) { if (ev.key === "Escape") closePops(null); });

  // More actions hangs from its button. Reserve only the part of the menu that
  // would otherwise stick out of the bar and cover the fit and rules cards.
  function reserveMore(d) {
    var bar = d.closest(".next-action");
    var panel = d.querySelector(".pop-body");
    if (!bar || !panel) return;
    if (!d.open) { bar.style.marginBottom = ""; return; }
    var hang = (d.getBoundingClientRect().bottom + 8 + panel.offsetHeight) - bar.getBoundingClientRect().bottom;
    bar.style.marginBottom = hang > 0 ? Math.ceil(hang) + "px" : "";
    panel.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
  document.querySelectorAll("details.more").forEach(function (d) {
    d.addEventListener("toggle", function () { reserveMore(d); });
    if (d.open) reserveMore(d);
  });
  window.addEventListener("resize", function () {
    document.querySelectorAll("details.more[open]").forEach(reserveMore);
  });

  // Double-submit guard for state-changing forms.
  document.querySelectorAll("form[method='post']").forEach(function (form) {
    form.addEventListener("submit", function () {
      if (form.classList.contains("theme-form")) return;
      var btn = form.querySelector("button[type='submit'], button:not([type])");
      if (btn) setTimeout(function () { btn.disabled = true; }, 0);
    });
  });
})();
