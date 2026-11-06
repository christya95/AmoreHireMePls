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

  // Application page: refresh saved steps about every four seconds, and this job's network rows about once a second, while the task is live.
  function networkPiece(row) {
    var status = row.status === 0 || row.status ? String(row.status) : "";
    return [row.id || "", row.method || "", status, row.timing || "", row.type || "", row.url || "", row.body || ""].join("\t");
  }
  function networkSig(rows) {
    return (rows || []).map(networkPiece).join("\n");
  }
  function renderNetworkRow(row) {
    var el = document.createElement("div");
    el.className = "list-item";
    el.setAttribute("data-id", row.id || "");
    el.setAttribute("data-piece", networkPiece(row));
    var when = document.createElement("span");
    when.className = "t";
    when.textContent = row.at || "";
    var what = document.createElement("span");
    what.className = "msg";
    var line = document.createElement("span");
    line.className = "line";
    var status = row.status === 0 || row.status ? String(row.status) : "—";
    line.textContent = [row.method || "", status, row.type || ""].filter(Boolean).join(" ") + (row.timing ? " · " + row.timing : "");
    var url = document.createElement("span");
    url.className = "url";
    url.textContent = row.url || "";
    what.appendChild(line);
    what.appendChild(url);
    if (row.body) {
      var body = document.createElement("span");
      body.className = "body";
      body.textContent = row.body;
      what.appendChild(body);
    }
    el.appendChild(when);
    el.appendChild(what);
    return el;
  }
  function paintNetwork(rows) {
    var box = document.getElementById("network-lines");
    var empty = document.getElementById("network-empty");
    if (!box) return;
    var sig = networkSig(rows);
    if (box.getAttribute("data-sig") === sig) return;
    var y = window.scrollY;
    var have = {};
    Array.prototype.forEach.call(box.children, function (el) {
      var id = el.getAttribute("data-id");
      if (id) have[id] = el;
    });
    var frag = document.createDocumentFragment();
    (rows || []).forEach(function (row) {
      var id = row.id || "";
      var piece = networkPiece(row);
      var el = have[id];
      if (el && el.getAttribute("data-piece") === piece) frag.appendChild(el);
      else frag.appendChild(renderNetworkRow(row));
    });
    box.replaceChildren(frag);
    box.setAttribute("data-sig", sig);
    if (empty) empty.hidden = !!(rows && rows.length);
    window.scrollTo(0, y);
  }
  function paintFlags(data) {
    var banner = document.getElementById("activity-banner");
    if (banner) banner.classList.toggle("working", !!data.working);
    var state = document.getElementById("activity-state");
    if (state && data.state) state.textContent = data.state;
    var dots = document.getElementById("activity-dots");
    if (dots) dots.hidden = !data.working;
    var now = document.getElementById("activity-now");
    if (now && data.now !== undefined) {
      now.hidden = !data.now;
      if (data.now && now.textContent !== data.now) now.textContent = data.now;
    }
    var fieldBox = document.getElementById("field-answer");
    if (data.field && data.field.label) {
      if (!fieldBox) {
        window.location.reload();
        return;
      }
      var fieldLabel = document.getElementById("field-answer-label");
      var fieldReason = document.getElementById("field-answer-reason");
      if (fieldLabel) fieldLabel.textContent = data.field.label;
      if (fieldReason) fieldReason.textContent = data.field.reason || "";
      if (banner) banner.hidden = true;
    } else if (fieldBox) {
      fieldBox.remove();
    }
  }
  function paintSteps(data) {
    var box = document.getElementById("activity-lines");
    if (!box) return;
    box.replaceChildren();
    (data.lines || []).forEach(function (line) {
      var row = document.createElement("div");
      row.className = "list-item";
      var when = document.createElement("span");
      when.className = "t";
      when.textContent = line.at || "";
      var what = document.createElement("span");
      what.textContent = line.text + (line.count > 1 ? " (" + line.count + ")" : "");
      row.appendChild(when);
      row.appendChild(what);
      box.appendChild(row);
    });
  }
  var networkBox = document.getElementById("network-lines");
  var networkSeed = document.getElementById("network-seed");
  if (networkBox && networkSeed) {
    try { networkBox.setAttribute("data-sig", networkSig(JSON.parse(networkSeed.textContent || "[]"))); } catch (err) { /* the next pass paints */ }
  }
  var activity = document.getElementById("updates");
  var networkCard = document.getElementById("network");
  var liveNode = activity && activity.getAttribute("data-live") === "1" ? activity : (networkCard && networkCard.getAttribute("data-live") === "1" ? networkCard : null);
  if (liveNode) {
    var ticks = 0;
    var activityTimer = setInterval(function () {
      fetch(liveNode.getAttribute("data-activity"), { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (data) {
          if (!data) return;
          if (data.network_open) {
            var tab = document.getElementById("network-tab");
            if (tab) tab.open = true;
          }
          paintNetwork(data.network || []);
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
})();
