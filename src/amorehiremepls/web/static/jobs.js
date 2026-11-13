// Jobs page only: "Check for new jobs" without leaving the page. Progressive enhancement: without this file the
// form posts, the page reloads, and the strip above the list says the same sentences.
//
// Press -> the button is disabled and says "Checking…", the rows dim and a bar sweeps under the strip.
// While the check waits or runs -> the strip is read again every 1.5 s (every 5 s while it has not started).
// When it ends -> the rows are loaded again and fade up one after another, jobs it found carry a New mark,
// the strip says what was found, and a short banner repeats it. The stylesheet drops the movement under reduced
// motion; the words, the dimming and the marks stay.
(function () {
  "use strict";
  var strip = document.getElementById("jobs-refresh");
  var list = document.getElementById("jobs-list");
  if (!strip || !list || !window.fetch) return;

  var card = strip.closest(".card");
  var stateUrl = strip.getAttribute("data-state");
  var listUrl = strip.getAttribute("data-list");
  var ICONS = { ok: "check", warn: "alert", bad: "alert", info: "sync", neutral: "history" };
  var FAST = 1500, SLOW = 5000, ROW_STEP_CAP = 12;
  var pollTimer = null, busy = false, banner = null, bannerTimer = null;

  function byId(id) { return document.getElementById(id); }
  function setText(el, text) { if (el && el.textContent !== text) el.textContent = text; }

  // "just now", "3 minutes ago", "yesterday": the same words the server writes, kept current while the page is open.
  function ago(iso) {
    var then = Date.parse(iso);
    if (isNaN(then)) return "";
    var seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
    if (seconds < 45) return "just now";
    var minutes = Math.max(1, Math.round(seconds / 60));
    if (minutes < 60) return minutes + (minutes === 1 ? " minute ago" : " minutes ago");
    var hours = Math.round(minutes / 60);
    if (hours < 24) return hours + (hours === 1 ? " hour ago" : " hours ago");
    var days = Math.round(hours / 24);
    return days === 1 ? "yesterday" : days + " days ago";
  }
  function tickTime() {
    var time = byId("jr-time");
    if (time && time.getAttribute("datetime")) setText(time, ago(time.getAttribute("datetime")));
  }
  setInterval(tickTime, 30000);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) tickTime(); });

  function buttons() { return document.querySelectorAll("[data-jobs-check] button"); }
  function setBusy(on) {
    busy = on;
    if (card) card.classList.toggle("checking", on);
    list.classList.toggle("refreshing", on);
    if (on) list.setAttribute("aria-busy", "true"); else list.removeAttribute("aria-busy");
    buttons().forEach(function (btn) {
      var label = btn.querySelector(".jr-label");
      // Hold the width so the row does not shift when the words change.
      if (on && !btn.style.minWidth) btn.style.minWidth = btn.offsetWidth + "px";
      if (!on) btn.style.minWidth = "";
      btn.disabled = on;
      if (label) setText(label, label.getAttribute(on ? "data-busy" : "data-idle") || label.textContent);
    });
  }

  function paintWords(tone, line, detail) {
    strip.setAttribute("data-tone", tone || "neutral");
    var icon = byId("jr-icon");
    if (icon) icon.setAttribute("href", "#i-" + (ICONS[tone] || ICONS.neutral));
    setText(byId("jr-line"), line || "");
    setText(byId("jr-detail"), detail || "");
  }
  function paint(state) {
    paintWords(state.tone, state.line, state.detail);
    var when = byId("jr-when"), time = byId("jr-time");
    if (when && time) {
      when.hidden = !state.checked_at;
      setText(byId("jr-when-label"), state.when_label || "Last checked");
      time.setAttribute("datetime", state.checked_at || "");
      time.setAttribute("title", state.checked_title || "");
      tickTime();
    }
  }

  function showBanner(text, tone) {
    // The same .flash the server uses, in the same place. Good news leaves by itself; a failure stays until the next check.
    hideBanner(true);
    var main = document.querySelector(".main > main");
    if (!main || !text) return;
    banner = document.createElement("div");
    banner.className = "flash" + (tone === "bad" ? " bad" : tone === "warn" ? " warn" : "");
    banner.setAttribute("role", tone === "bad" ? "alert" : "status");
    banner.textContent = text;
    main.parentNode.insertBefore(banner, main);
    if (tone !== "bad") bannerTimer = setTimeout(function () { hideBanner(false); }, tone === "warn" ? 10000 : 6000);
  }
  function hideBanner(now) {
    if (bannerTimer) { clearTimeout(bannerTimer); bannerTimer = null; }
    var el = banner;
    banner = null;
    if (!el) return;
    if (now) { el.remove(); return; }
    el.classList.add("leaving");
    setTimeout(function () { el.remove(); }, 220);
  }

  function getJson(url, options) {
    var opts = options || {};
    opts.credentials = "same-origin";
    opts.headers = { Accept: "application/json", "X-Requested-With": "fetch" };
    return fetch(url, opts).then(function (response) {
      if (!response.ok) throw new Error("status " + response.status);
      return response.json();
    });
  }

  function replayRows() {
    // Company logos swapped in after load need the same fallback app.js gives the first page: initials when the image fails.
    list.querySelectorAll(".avatar img.logo").forEach(function (img) {
      img.addEventListener("error", function () { var holder = img.closest(".avatar"); if (holder) holder.classList.remove("has-logo"); });
    });
    list.querySelectorAll("tbody tr").forEach(function (row, index) {
      row.style.setProperty("--i", String(Math.min(index, ROW_STEP_CAP)));
    });
    list.classList.remove("reloaded");
    void list.offsetWidth;
    list.classList.add("reloaded");
    strip.classList.remove("settled");
    void strip.offsetWidth;
    strip.classList.add("settled");
    setTimeout(function () { list.classList.remove("reloaded"); strip.classList.remove("settled"); }, 3400);
  }

  function reloadList(state) {
    return fetch(listUrl, { credentials: "same-origin", headers: { Accept: "text/html", "X-Requested-With": "fetch" } })
      .then(function (response) {
        if (!response.ok) throw new Error("status " + response.status);
        return response.text();
      })
      .then(function (html) {
        list.innerHTML = html;
        setBusy(false);
        paint(state);
        replayRows();
        showBanner((state.line + " " + (state.detail || "")).trim(), state.tone);
      })
      .catch(function () {
        setBusy(false);
        paint(state);
        showBanner("The check ended, but the list could not be loaded again. Reload this page to see the jobs.", "bad");
      });
  }

  function schedule(delay) {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = setTimeout(poll, delay);
  }
  function poll() {
    pollTimer = null;
    if (document.hidden) { schedule(SLOW); return; }
    getJson(stateUrl)
      .then(function (state) {
        if (state.checking) {
          paint(state);
          schedule(state.stalled ? SLOW : FAST);
          return;
        }
        return reloadList(state);
      })
      .catch(function () { schedule(SLOW); /* the next pass tries again */ });
  }

  // One listener for every check form, including the one inside the empty list, which is swapped in later.
  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (!form || !form.matches || !form.matches("form[data-jobs-check]")) return;
    ev.preventDefault();
    if (busy) return;
    hideBanner(true);
    setBusy(true);
    paintWords("info", "Starting the check.", "");
    getJson(form.getAttribute("action"), { method: "POST", body: new URLSearchParams(new FormData(form)) })
      .then(function (state) {
        if (state.checking) { paint(state); schedule(FAST); return; }
        return reloadList(state); // it had already ended by the time the answer came back
      })
      .catch(function () {
        setBusy(false);
        getJson(stateUrl).then(paint).catch(function () { /* the strip keeps its last words */ });
        showBanner("The check could not be started. Reload this page and try again.", "bad");
      });
  });

  // A check that was already under way when the page opened (asked for earlier, or started by the schedule).
  if (strip.getAttribute("data-checking") === "1") { setBusy(true); schedule(FAST); }
  tickTime();
})();
