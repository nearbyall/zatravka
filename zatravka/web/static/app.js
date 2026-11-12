(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  async function post(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || data.error || `Ошибка ${res.status}`);
    return data;
  }

  let toastTimer;
  function toast(text) {
    const el = $("#toast");
    el.textContent = text;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (el.hidden = true), 2600);
  }

  // Theme follows the system until the user picks one, which is remembered.
  $("#theme-btn")?.addEventListener("click", () => {
    const root = document.documentElement;
    const dark = root.dataset.theme
      ? root.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("theme", root.dataset.theme); } catch (e) {}
  });

  $$("[data-autosubmit]").forEach((el) => el.addEventListener("change", () => el.form.requestSubmit()));
  // The "and global" checkbox writes 1/0 to a hidden field so the unchecked state is submitted too.
  $$("[data-glob-toggle]").forEach((cb) => cb.addEventListener("change", () => {
    $("[data-glob]", cb.form).value = cb.checked ? "1" : "0";
    cb.form.requestSubmit();
  }));

  document.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("[data-status]");
    if (!btn) return;
    const group = btn.closest("[data-idea]");
    const id = group.dataset.idea;
    const status = btn.dataset.status;
    if (group.dataset.busy) return;
    group.dataset.busy = "true";
    $$("button", group).forEach((b) => (b.disabled = true));
    try {
      const res = await post(`/api/ideas/${id}/status`, { status });
      const row = btn.closest("[data-row]");
      if (group.classList.contains("status-compact")) {
        const star = $('[aria-label="В избранное"]', group);
        const starred = status === "starred";
        star.setAttribute("aria-pressed", String(starred));
        star.dataset.status = starred ? "new" : "starred";
        row?.classList.toggle("is-starred", starred);
        row?.classList.toggle("is-gone", status === "rejected");
        toast(status === "rejected" ? "Идея отклонена" : starred ? "Добавлено в избранное" : "Убрано из избранного");
      } else {
        $$("[data-status]", group).forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
        toast(`Статус: ${res.label}`);
      }
    } catch (e) {
      toast(e.message);
    } finally {
      delete group.dataset.busy;
      $$("button", group).forEach((b) => (b.disabled = false));
    }
  });

  // Notes save on blur or a second after typing stops.
  $$("[data-note]").forEach((ta) => {
    let timer;
    const saved = $("[data-saved]");
    const save = async () => {
      try {
        await post(`/api/ideas/${ta.dataset.note}/note`, { note: ta.value });
        if (saved) { saved.hidden = false; setTimeout(() => (saved.hidden = true), 1800); }
      } catch (e) { toast(e.message); }
    };
    ta.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(save, 1000); });
    ta.addEventListener("blur", () => { clearTimeout(timer); save(); });
  });

  $$("[data-toggle-direction]").forEach((sw) =>
    sw.addEventListener("click", async () => {
      sw.disabled = true;
      try {
        const res = await post(`/api/directions/${sw.dataset.toggleDirection}/toggle`);
        sw.setAttribute("aria-checked", String(res.enabled));
        sw.closest("tr")?.classList.toggle("is-off", !res.enabled);
      } catch (e) { toast(e.message); }
      finally { sw.disabled = false; }
    })
  );

  const runBtn = $("#run-btn");
  const log = $("#runlog");
  let polling = null;

  function fmt(sec) {
    if (sec == null) return "";
    const m = Math.floor(sec / 60), s = sec % 60;
    return m ? `${m} мин ${s} с` : `${s} с`;
  }

  async function poll() {
    const res = await fetch("/api/run").then((r) => r.json()).catch(() => null);
    if (!res) return;
    $("#runlog-lines").textContent = res.lines.join("\n") || "Готовлю план прогона…";
    $("#runlog-lines").scrollTop = 1e9;
    $("#runlog-time").textContent = fmt(res.elapsed);
    if (res.running) {
      runBtn.dataset.running = "";
      runBtn.textContent = "Прогон идёт";
      return;
    }
    clearInterval(polling);
    polling = null;
    delete runBtn.dataset.running;
    runBtn.textContent = "Запустить прогон";
    const r = res.result || {};
    const ideas = r.stats?.extract?.ideas || 0;
    $("#runlog-title").textContent =
      r.status === "ok" ? `Прогон готов: новых идей ${ideas}` :
      r.status === "budget" ? `Бюджет на сегодня исчерпан, идей ${ideas}` :
      r.status === "busy" ? "Уже идёт другой прогон (демон)" :
      `Прогон завершился: ${r.status || "ошибка"}`;
    $("#runlog-foot").hidden = false;
  }

  async function startRun() {
    if (runBtn.disabled) return;
    runBtn.disabled = true;
    try {
      await post("/api/run", { collect: true });
    } catch (e) {
      if (!String(e.message).includes("уже идёт")) return toast(e.message);
    } finally {
      runBtn.disabled = false;
    }
    $("#runlog-title").textContent = "Прогон идёт";
    $("#runlog-foot").hidden = true;
    log.hidden = false;
    if (!polling) polling = setInterval(poll, 3000);
    poll();
  }

  runBtn?.addEventListener("click", () => (runBtn.hasAttribute("data-running") ? (log.hidden = false) : startRun()));
  $$("[data-run]").forEach((b) => b.addEventListener("click", startRun));
  $("#runlog-close")?.addEventListener("click", () => (log.hidden = true));
  if (runBtn?.hasAttribute("data-running")) { log.hidden = false; polling = setInterval(poll, 3000); poll(); }
})();
