(function () {
  "use strict";

  async function post(url, csrf, body) {
    const res = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify(body || {}),
    });
    if (res.status === 401) { window.location.href = "/login"; return null; }
    if (!res.ok) throw new Error("Request failed: " + res.status);
    return res.json();
  }

  const list = document.getElementById("items");
  if (list) {
    const csrf = list.dataset.csrf;
    list.addEventListener("change", async (ev) => {
      const box = ev.target;
      if (!box.classList.contains("sync-toggle")) return;
      const li = box.closest(".item");
      li.classList.add("saving");
      try {
        const out = await post("/api/items/" + encodeURIComponent(box.dataset.id) + "/sync", csrf, { enabled: box.checked });
        if (out) {
          li.dataset.synced = out.enabled ? "1" : "0";
          const badge = li.querySelector(".badge.new");
          if (badge) badge.remove();
          li.dataset.new = "0";
        }
      } catch (e) {
        box.checked = !box.checked;
        alert("Couldn't save that change. Check your connection and try again.");
      } finally {
        li.classList.remove("saving");
      }
    });

    const search = document.getElementById("search");
    const tabs = document.querySelectorAll(".tab");
    const empty = document.getElementById("empty-filter");
    let filter = "all";
    function apply() {
      const q = (search.value || "").trim().toLowerCase();
      let shown = 0;
      for (const li of list.children) {
        const okKind = filter === "all" || (filter === "synced" ? li.dataset.synced === "1"
          : filter === "new" ? li.dataset.new === "1" : li.dataset.kind === filter);
        const okText = !q || li.dataset.text.includes(q);
        li.hidden = !(okKind && okText);
        if (!li.hidden) shown++;
      }
      empty.hidden = shown !== 0;
    }
    tabs.forEach((t) => t.addEventListener("click", () => {
      tabs.forEach((x) => x.setAttribute("aria-selected", x === t ? "true" : "false"));
      filter = t.dataset.filter;
      apply();
    }));
    search.addEventListener("input", apply);
    const wanted = new URLSearchParams(window.location.search).get("filter");
    const tab = wanted && document.querySelector('.tab[data-filter="' + wanted + '"]');
    if (tab) tab.click();
  }

  const reviewAll = document.querySelector("[data-review-all]");
  if (reviewAll) {
    reviewAll.addEventListener("click", async () => {
      await post("/api/items/review-all", reviewAll.dataset.csrf);
      document.querySelectorAll(".badge.new").forEach((b) => b.remove());
      document.getElementById("new-banner").remove();
    });
  }
})();

(function () {
  "use strict";
  const bar = document.getElementById("statusbar");
  if (bar && bar.dataset.running === "1") {
    const progress = document.getElementById("sync-progress");
    const timer = setInterval(async () => {
      try {
        const res = await fetch("/api/status", { credentials: "same-origin" });
        if (!res.ok) return;
        const st = await res.json();
        if (!st.running) { clearInterval(timer); window.location.replace("/"); return; }
        const p = st.progress || {};
        progress.textContent = (p.phase || "") + (p.total ? " " + p.done + "/" + p.total + (p.current ? " · " + p.current : "") : "");
      } catch (e) { /* keep polling */ }
    }, 4000);
  }

  const all = document.getElementById("check-all");
  if (all) {
    all.addEventListener("change", () => {
      document.querySelectorAll('#removals-form input[name="ids"]').forEach((c) => { c.checked = all.checked; });
    });
  }
})();

document.querySelectorAll("[data-confirm]").forEach((btn) => {
  btn.addEventListener("click", (ev) => {
    if (!window.confirm(btn.dataset.confirm)) ev.preventDefault();
  });
});

(function () {
  "use strict";
  const box = document.getElementById("push-box");
  if (!box) return;
  const csrf = box.dataset.csrf;
  const status = document.getElementById("push-status");
  const enable = document.getElementById("push-enable");
  const disable = document.getElementById("push-disable");

  function b64ToBytes(b64) {
    const pad = "=".repeat((4 - (b64.length % 4)) % 4);
    const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  }
  function say(msg) { status.textContent = msg; }

  if (!window.isSecureContext) {
    say("Notifications need HTTPS. Open YTMLS through your Cloudflare Tunnel address to turn them on.");
    return;
  }
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    say("This browser doesn't support push notifications. On iPhone or iPad, add YTMLS to your Home Screen first.");
    return;
  }

  async function current() {
    const reg = await navigator.serviceWorker.register("/sw.js", { scope: "/" });
    await navigator.serviceWorker.ready;
    return { reg, sub: await reg.pushManager.getSubscription() };
  }

  async function refresh() {
    try {
      const { sub } = await current();
      enable.hidden = !!sub;
      disable.hidden = !sub;
      say(sub ? "This device gets notifications." : (Notification.permission === "denied"
        ? "Notifications are blocked for this site in your browser settings."
        : "This device doesn't get notifications yet."));
    } catch (e) { say("Couldn't check notification status: " + e.message); }
  }

  enable.addEventListener("click", async () => {
    try {
      const perm = await Notification.requestPermission();
      if (perm !== "granted") { say("Permission wasn't granted."); return; }
      const { reg } = await current();
      const keyRes = await fetch("/api/push/key", { credentials: "same-origin" });
      const { key } = await keyRes.json();
      const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(key) });
      const res = await fetch("/api/push/subscribe", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({ subscription: sub.toJSON() }),
      });
      if (!res.ok) { const err = await res.json().catch(() => ({})); throw new Error(err.error || res.status); }
      window.location.reload();
    } catch (e) { say("Couldn't turn on notifications: " + e.message); }
  });

  disable.addEventListener("click", async () => {
    try {
      const { sub } = await current();
      if (sub) {
        await fetch("/api/push/unsubscribe", {
          method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
          body: JSON.stringify({ endpoint: sub.endpoint }),
        });
        await sub.unsubscribe();
      }
      window.location.reload();
    } catch (e) { say("Couldn't turn off notifications: " + e.message); }
  });

  refresh();
})();

document.querySelectorAll('img[src^="/covers/"]').forEach((img) => {
  const hide = () => { img.style.visibility = "hidden"; };
  if (img.complete && img.naturalWidth === 0) hide();
  img.addEventListener("error", hide);
});
