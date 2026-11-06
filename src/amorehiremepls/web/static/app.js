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

  // Application page: refresh the status block while a task is live, and the saved steps about every four seconds.
  var IDLE_LINE = "Nothing is being filled in right now.";
  var reloadTimer = null;
  function reloadSoon(delay) {
    // The block's control depends on the saved state, so a changed status reloads the page once the pulse has shown.
    if (reloadTimer) return;
    reloadTimer = setTimeout(function () { window.location.reload(); }, delay);
  }
  function pulse(el) {
    // One ring, once. Never on first paint: this runs only when the poll saw a different value.
    if (!el) return;
    el.classList.remove("pulse");
    void el.offsetWidth;
    el.classList.add("pulse");
    el.addEventListener("animationend", function () { el.classList.remove("pulse"); }, { once: true });
  }
  function paintChip(banner, data) {
    if (!data.status) return false;
    var chip = banner.querySelector(".status");
    if (!chip) return false;
    var raw = chip.querySelector(".raw");
    var pill = chip.querySelector(".pill");
    if (!raw || !pill || raw.textContent === data.status) return false;
    raw.textContent = data.status;
    pill.textContent = data.status_label || data.status;
    pill.className = "pill " + (data.tone || "neutral");
    banner.setAttribute("data-tone", data.tone || "neutral");
    pulse(pill);
    return true;
  }
  function paintFlags(data) {
    var banner = document.getElementById("activity-banner");
    if (!banner) return;
    banner.classList.toggle("working", !!data.working);
    var state = document.getElementById("activity-state");
    if (state && data.state) {
      var hideIdle = data.state === IDLE_LINE && banner.getAttribute("data-idle-hidden") === "1";
      state.hidden = hideIdle;
      if (!hideIdle && state.textContent !== data.state) state.textContent = data.state;
    }
    var dots = document.getElementById("activity-dots");
    if (dots) dots.hidden = !data.working;
    var flash = document.querySelector(".main > .flash");
    var settled = data.state && /was typed as|No list of places appeared|so the form is waiting/.test(data.state);
    if (flash && settled && /will be typed/.test(flash.textContent || "")) flash.remove();
    var fieldBox = document.getElementById("field-answer");
    if (data.field && data.field.label) {
      if (!fieldBox) { reloadSoon(0); return; }
      var fieldLabel = document.getElementById("field-answer-label");
      if (fieldLabel && fieldLabel.textContent !== data.field.label) fieldLabel.textContent = data.field.label;
      var fieldInput = fieldBox.querySelector("label");
      if (fieldInput && fieldInput.firstChild && fieldInput.firstChild.nodeType === 3) fieldInput.firstChild.textContent = data.field.label + " ";
    } else if (fieldBox) {
      reloadSoon(0);
      return;
    }
    if (paintChip(banner, data)) reloadSoon(900);
  }
  function stepRow(line, fresh) {
    var row = document.createElement("div");
    row.className = "list-item " + (fresh ? "enter" : "settled");
    row.setAttribute("data-key", (line.at || "") + "|" + line.text);
    var when = document.createElement("span");
    when.className = "t";
    when.textContent = line.at || "";
    var what = document.createElement("span");
    what.textContent = line.text + (line.count > 1 ? " (" + line.count + ")" : "");
    row.appendChild(when);
    row.appendChild(what);
    return row;
  }
  function paintSteps(data) {
    // Rows the page already shows keep still; only a row new to the page slides in from the top.
    var box = document.getElementById("activity-lines");
    if (!box) return;
    var known = {};
    box.querySelectorAll(".list-item[data-key]").forEach(function (row) { known[row.getAttribute("data-key")] = true; });
    var lines = data.lines || [];
    var changed = lines.length !== Object.keys(known).length || lines.some(function (line) { return !known[(line.at || "") + "|" + line.text]; });
    if (!changed) return;
    var next = document.createDocumentFragment();
    lines.forEach(function (line) { next.appendChild(stepRow(line, !known[(line.at || "") + "|" + line.text])); });
    box.replaceChildren(next);
    var empty = box.parentNode && box.parentNode.querySelector(".note");
    if (empty && lines.length && /Nothing has happened yet/.test(empty.textContent || "")) empty.remove();
  }
  var activity = document.getElementById("updates");
  if (activity && activity.getAttribute("data-live") === "1") {
    var ticks = 0;
    var activityTimer = setInterval(function () {
      fetch(activity.getAttribute("data-activity"), { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (data) {
          if (!data) return;
          ticks += 1;
          paintFlags(data);
          if (ticks % 4 === 0) paintSteps(data);
          if (!data.live) clearInterval(activityTimer);
        })
        .catch(function () { /* the next pass tries again */ });
    }, 1000);
  }

  // Copy the exact text of a details block. A secure page (localhost) tries the clipboard API first.
  // A plain HTTP dashboard has no clipboard API, so a temporary text box and execCommand cover that case.
  function legacyCopy(text) {
    return new Promise(function (resolve, reject) {
      var area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.cssText = "position:fixed;top:0;left:0;width:2em;height:2em;padding:0;border:0;outline:none;opacity:0";
      document.body.appendChild(area);
      area.focus();
      area.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
      document.body.removeChild(area);
      if (ok) resolve(); else reject();
    });
  }
  function copyText(text) {
    if (window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).catch(function () { return legacyCopy(text); });
    }
    return legacyCopy(text);
  }
  document.querySelectorAll("button[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function (ev) {
      ev.preventDefault();
      ev.stopPropagation();
      var pre = document.getElementById(btn.getAttribute("data-copy") || "");
      function flash(label) {
        btn.textContent = label;
        if (btn._copyTimer) clearTimeout(btn._copyTimer);
        btn._copyTimer = setTimeout(function () { btn.textContent = "Copy"; }, 1600);
      }
      if (!pre) { flash("Copy failed"); return; }
      copyText(pre.textContent).then(function () { flash("Copied"); }, function () { flash("Copy failed"); });
    });
  });

  // Double-submit guard for state-changing forms.
  document.querySelectorAll("form[method='post']").forEach(function (form) {
    form.addEventListener("submit", function () {
      if (form.classList.contains("theme-form")) return;
      var btn = form.querySelector("button[type='submit'], button:not([type])");
      if (btn) setTimeout(function () { btn.disabled = true; }, 0);
    });
  });

  // Restart entrance motion on the first frame that can paint. The stylesheet
  // animation otherwise starts while the document is still parsing, and an
  // ease-out has already settled by the time the first frame is drawn.
  // Reduced motion is left alone. The fill dots are not in this list.
  var entrance = ".sidebar, .topbar, .page-head, .empty, .login-card, .flash, .main .card, .stack > .card, .stack > .next-action, .kpis > .kpi, .table-wrap tbody tr, .timeline > .list-item, .fit-ring .value, .fit-bar .seg";
  function replayEntrance() {
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    var nodes = document.querySelectorAll(entrance);
    nodes.forEach(function (el) { el.style.animation = "none"; });
    if (document.body) void document.body.offsetWidth;
    nodes.forEach(function (el) { el.style.animation = ""; });
  }
  requestAnimationFrame(function () { requestAnimationFrame(replayEntrance); });
  window.addEventListener("pageshow", function (ev) { if (ev.persisted) replayEntrance(); });
})();
