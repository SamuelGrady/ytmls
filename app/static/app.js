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
        const okKind = filter === "all" || (filter === "synced" ? li.dataset.synced === "1" : li.dataset.kind === filter);
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
