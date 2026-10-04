(() => {
  const $ = (id) => document.getElementById(id);

  const STAGES = [
    "build",
    "prepare",
    "configure",
    "repair",
    "start",
    "provision",
    "health",
    "done",
  ];

  const STAGE_LABELS = {
    build: "Building installer…",
    prepare: "Preparing…",
    configure: "Writing configuration…",
    repair: "Repairing containers…",
    start: "Starting services…",
    provision: "Creating admin account…",
    health: "Checking health…",
    done: "Finishing…",
  };

  const state = {
    status: null,
    requirements: null,
    config: null,
    installing: false,
    showLog: true,
    targetLevel: 1,
    stageIndex: -1,
  };

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: {
        Accept: "application/json",
        ...(opts.body ? { "Content-Type": "application/json" } : {}),
      },
      ...opts,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(data.error || res.statusText || "Request failed");
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function setProgress(pct, label) {
    const clamped = Math.max(0, Math.min(100, Math.round(pct)));
    $("barFill").style.width = `${clamped}%`;
    $("progressPct").textContent = `${clamped}%`;
    if (label) $("stageLabel").textContent = label;
  }

  function advanceStage(name) {
    const idx = STAGES.indexOf(name);
    if (idx < 0) return;
    if (idx < state.stageIndex) return;
    state.stageIndex = idx;
    const pct = ((idx + 1) / STAGES.length) * 100;
    setProgress(pct, STAGE_LABELS[name] || name);
  }

  function renderStair() {
    const root = $("stair");
    const current = state.status?.level ?? 0;
    const steps = state.status?.steps || [];
    root.innerHTML = steps
      .map((step) => {
        const done = current > step.level;
        const isCurrent = current === step.level;
        const cls = ["riser", done ? "done" : "", isCurrent ? "current" : ""]
          .filter(Boolean)
          .join(" ");
        const items = (step.includes || [])
          .map((x) => `<li>${escapeHtml(x)}</li>`)
          .join("");
        return `<article class="${cls}" data-level="${step.level}">
          <div class="lvl">Level ${step.level}</div>
          <h3>${escapeHtml(step.title)}</h3>
          <ul>${items}</ul>
        </article>`;
      })
      .join("");
  }

  function updateInstallButton() {
    const btn = $("btnInstall");
    const un = $("btnUninstall");
    const hint = $("installHint");
    const level = state.status?.level ?? 0;
    const reqsOk = !!state.requirements?.ok;
    const keyOk = !!state.config?.provider_configured;
    const dockerOk = (state.requirements?.checks || []).some(
      (c) => c.id === "docker_daemon" && c.ok
    );

    let target = 1;
    let label = "Install Standard";
    if (level === 0) {
      target = 1;
      label = "Install Standard";
      hint.textContent = "Installs Level 1 — chat ready to use.";
    } else if (level === 1) {
      target = 2;
      label = "Upgrade to Complete";
      hint.textContent = "Adds tools, automation, and observability.";
    } else {
      target = 2;
      label = "Repair install";
      hint.textContent = "Re-run without wiping data.";
    }

    state.targetLevel = target;
    btn.textContent = label;
    btn.disabled = state.installing || !reqsOk || !keyOk;
    // Always allow erase when Docker works (no-op if nothing is installed).
    un.disabled = state.installing || !dockerOk;

    if (!reqsOk) hint.textContent = "Fix requirements before installing.";
    else if (!keyOk) hint.textContent = "Add an AI provider key in Configure.";
  }

  function renderRequirements() {
    const r = state.requirements;
    if (!r) return;
    $("reqsBadge").textContent = r.ok ? "Ready" : "Fix these";
    $("reqsBadge").className = `badge ${r.ok ? "ok" : "bad"}`;
    $("reqList").innerHTML = (r.checks || [])
      .map((c) => {
        const fail = !c.ok;
        return `<li class="${fail ? "fail" : ""}">
          <span class="name">${escapeHtml(c.label)}</span>
          <span class="detail">${escapeHtml(c.detail || "")}</span>
        </li>`;
      })
      .join("");
    updateInstallButton();
  }

  function renderConfigForm() {
    const root = $("configGroups");
    const groups = state.config?.groups || [];
    root.innerHTML = groups
      .map((g) => {
        const fields = (g.fields || [])
          .map((f) => {
            const secret = !!f.secret;
            const val = secret ? "" : f.value || "";
            const ph = f.placeholder || "";
            return `<div class="field ${secret ? "secret" : ""}">
              <label for="cfg-${escapeHtml(f.key)}">${escapeHtml(f.label)}</label>
              <input
                id="cfg-${escapeHtml(f.key)}"
                name="${escapeHtml(f.key)}"
                type="${secret ? "password" : "text"}"
                autocomplete="off"
                data-secret="${secret ? "1" : "0"}"
                placeholder="${escapeHtml(ph)}"
                value="${escapeHtml(val)}"
              />
            </div>`;
          })
          .join("");
        return `<section class="group">
          <h3>${escapeHtml(g.title)}</h3>
          <div class="group-body">${fields}</div>
        </section>`;
      })
      .join("");
  }

  function renderStatus() {
    const s = state.status;
    if (!s) return;
    $("statusLabel").textContent = s.labels?.[s.level] || "Unknown";
    const pkg = s.package_version || "—";
    const inst = s.installed_version ? `installed ${s.installed_version}` : "not versioned yet";
    $("pkgVersion").textContent = `${pkg} · ${inst}`;
    renderStair();
    updateInstallButton();
  }

  async function refresh() {
    const [status, requirements, config] = await Promise.all([
      api("/api/status"),
      api("/api/requirements"),
      api("/api/config"),
    ]);
    state.status = status;
    state.requirements = requirements;
    state.config = config;
    renderStatus();
    renderRequirements();
    renderConfigForm();
  }

  function openDrawer() {
    renderConfigForm();
    $("configStatus").textContent = "";
    $("configStatus").className = "form-status";
    $("configDrawer").classList.add("open");
    $("configDrawer").setAttribute("aria-hidden", "false");
    $("scrim").hidden = false;
  }

  function closeDrawer() {
    $("configDrawer").classList.remove("open");
    $("configDrawer").setAttribute("aria-hidden", "true");
    $("scrim").hidden = true;
  }

  function confirmAsk({
    title,
    message,
    okLabel = "Confirm",
    danger = false,
    extraHtml = "",
  }) {
    return new Promise((resolve) => {
      const dialog = $("confirmDialog");
      const form = $("confirmForm");
      const extra = $("confirmExtra");
      const btnOk = $("btnConfirmOk");
      const btnCancel = $("btnConfirmCancel");

      $("confirmTitle").textContent = title || "Confirm";
      $("confirmMessage").textContent = message || "";
      if (extraHtml) {
        extra.hidden = false;
        extra.innerHTML = extraHtml;
      } else {
        extra.hidden = true;
        extra.innerHTML = "";
      }
      btnOk.textContent = okLabel;
      btnOk.className = danger ? "btn danger" : "btn primary";

      const cleanup = () => {
        form.removeEventListener("submit", onSubmit);
        btnCancel.removeEventListener("click", onCancel);
        dialog.removeEventListener("cancel", onCancel);
      };
      const onSubmit = (e) => {
        e.preventDefault();
        cleanup();
        dialog.close();
        resolve(true);
      };
      const onCancel = (e) => {
        if (e) e.preventDefault();
        cleanup();
        dialog.close();
        resolve(false);
      };

      form.addEventListener("submit", onSubmit);
      btnCancel.addEventListener("click", onCancel);
      dialog.addEventListener("cancel", onCancel);
      dialog.showModal();
    });
  }

  function showAlert({ title, message, log, actions }) {
    $("alertTitle").textContent = title || "Notice";
    $("alertMessage").textContent = message || "";
    const logEl = $("alertLog");
    if (log) {
      logEl.hidden = false;
      logEl.textContent = log;
    } else {
      logEl.hidden = true;
      logEl.textContent = "";
    }
    const footer = $("alertActions");
    footer.innerHTML = "";
    const list = actions?.length ? actions : [{ id: "close", label: "OK" }];
    for (const a of list) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = a.id === "close" || a.id === "dismiss" ? "btn ghost" : "btn primary";
      b.textContent = a.label;
      b.addEventListener("click", () => {
        $("alertDialog").close();
        if (a.id === "configure") openDrawer();
        if (a.id === "retry_requirements") refresh().catch(console.error);
        if (a.id === "show_log") {
          state.showLog = true;
          $("logView").hidden = false;
          $("btnToggleLog").textContent = "Hide log";
          $("progressBlock").hidden = false;
        }
      });
      footer.appendChild(b);
    }
    if (!list.some((x) => x.id === "close" || x.id === "dismiss")) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "btn ghost";
      b.textContent = "Close";
      b.addEventListener("click", () => $("alertDialog").close());
      footer.appendChild(b);
    }
    $("alertDialog").showModal();
  }

  function beginJobUI(label) {
    state.installing = true;
    state.stageIndex = -1;
    updateInstallButton();
    $("progressBlock").hidden = false;
    $("logView").textContent = "";
    $("logView").hidden = !state.showLog;
    $("btnToggleLog").textContent = state.showLog ? "Hide log" : "Show log";
    setProgress(2, label);
  }

  function watchJobStream(onDone) {
    const es = new EventSource("/api/install/stream");
    es.onmessage = (ev) => {
      const data = ev.data || "";
      if (data.startsWith("{") && data.includes('"event": "done"')) {
        try {
          const done = JSON.parse(data);
          es.close();
          onDone(done);
          return;
        } catch (_) {
          /* treat as log */
        }
      }
      const m = /STAGE:\s*(\w+)/i.exec(data);
      if (m) advanceStage(m[1].toLowerCase());
      else if (data.startsWith("==>")) {
        // erase.sh progress lines
        const approx = Math.min(95, 10 + ($("logView").textContent.split("\n").length % 80));
        setProgress(approx, data.replace(/^==>\s*/, ""));
      }
      const view = $("logView");
      view.textContent += (view.textContent ? "\n" : "") + data;
      view.scrollTop = view.scrollHeight;
    };
    es.onerror = async () => {
      es.close();
      try {
        onDone(await api("/api/install/result"));
      } catch (e) {
        state.installing = false;
        updateInstallButton();
        showAlert({
          title: "Connection lost",
          message: "Check the terminal where you ran ./install.sh.",
        });
      }
    };
  }

  async function startInstall() {
    if (state.installing) return;
    beginJobUI("Starting installer…");

    try {
      await api("/api/install", {
        method: "POST",
        body: JSON.stringify({ level: state.targetLevel }),
      });
    } catch (e) {
      state.installing = false;
      updateInstallButton();
      showAlert({
        title: "Cannot start install",
        message: e.message || String(e),
        actions: [
          { id: "configure", label: "Configure" },
          { id: "retry_requirements", label: "Re-check" },
        ],
      });
      return;
    }

    watchJobStream(finishInstall);
  }

  async function startUninstall(wipeData) {
    if (state.installing) return;
    beginJobUI(wipeData ? "Uninstalling (wiping data)…" : "Uninstalling…");

    try {
      await api("/api/uninstall", {
        method: "POST",
        body: JSON.stringify({ wipe_data: !!wipeData }),
      });
    } catch (e) {
      state.installing = false;
      updateInstallButton();
      showAlert({
        title: "Cannot uninstall",
        message: e.message || String(e),
      });
      return;
    }

    watchJobStream(finishUninstall);
  }

  async function finishInstall(result) {
    state.installing = false;
    if (result.exit_code === 0) {
      setProgress(100, "Install complete");
      await refresh();
      showAlert({
        title: "All set",
        message:
          result.message ||
          "GenAI Stack is ready. Open http://localhost:3001 — password is in .env.",
        actions: [{ id: "close", label: "Done" }],
      });
    } else {
      $("stageLabel").textContent = "Install stopped";
      await refresh();
      showAlert({
        title: "Install needs a fix",
        message: result.message || "Something went wrong.",
        log: result.log_tail || "",
        actions: result.actions || [
          { id: "configure", label: "Configure" },
          { id: "show_log", label: "Show log" },
        ],
      });
    }
    updateInstallButton();
  }

  async function finishUninstall(result) {
    state.installing = false;
    if (result.exit_code === 0) {
      setProgress(100, "Uninstall complete");
      await refresh();
      showAlert({
        title: "Uninstalled",
        message: result.message || "Stack removed.",
        actions: [{ id: "close", label: "Done" }],
      });
    } else {
      $("stageLabel").textContent = "Uninstall stopped";
      await refresh();
      showAlert({
        title: "Uninstall failed",
        message: result.message || "Something went wrong.",
        log: result.log_tail || "",
        actions: [{ id: "show_log", label: "Show log" }],
      });
    }
    updateInstallButton();
  }

  $("btnConfigure").addEventListener("click", openDrawer);
  $("btnConfigClose").addEventListener("click", closeDrawer);
  $("btnConfigCancel").addEventListener("click", closeDrawer);
  $("scrim").addEventListener("click", closeDrawer);

  $("btnInstall").addEventListener("click", async () => {
    if (state.installing) return;
    const level = state.targetLevel;
    const label =
      level === 1
        ? "Install Standard (Level 1)"
        : state.status?.level === 2
          ? "Repair Complete install"
          : "Upgrade to Complete (Level 2)";
    const ok = await confirmAsk({
      title: "Confirm install",
      message: `${label}? This starts Docker and may take several minutes. Your data is not wiped.`,
      okLabel: level === 1 ? "Install" : state.status?.level === 2 ? "Repair" : "Upgrade",
    });
    if (ok) startInstall();
  });

  $("btnUninstall").addEventListener("click", async () => {
    if (state.installing) return;
    const ok = await confirmAsk({
      title: "Confirm uninstall",
      message:
        "Stop and remove GenAI Stack containers? Your .env file stays on disk.",
      okLabel: "Uninstall",
      danger: true,
      extraHtml: `<label class="check">
        <input type="checkbox" id="wipeData" />
        <span>Also wipe data volumes <strong>(chat history &amp; databases — permanent)</strong></span>
      </label>`,
    });
    if (!ok) return;
    const wipe = !!$("wipeData")?.checked;
    if (wipe) {
      const okWipe = await confirmAsk({
        title: "Wipe all data?",
        message:
          "This permanently deletes chat history and databases. This cannot be undone.",
        okLabel: "Wipe and uninstall",
        danger: true,
      });
      if (!okWipe) return;
    }
    startUninstall(wipe);
  });

  $("btnToggleLog").addEventListener("click", () => {
    state.showLog = !state.showLog;
    $("logView").hidden = !state.showLog;
    $("btnToggleLog").textContent = state.showLog ? "Hide log" : "Show log";
  });

  $("configForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const values = {};
    for (const input of $("configForm").querySelectorAll("input[name]")) {
      const val = input.value.trim();
      // Blank secrets/fields are omitted so "auto" generation still works.
      if (!val) continue;
      values[input.name] = val;
    }
    const ok = await confirmAsk({
      title: "Save configuration?",
      message:
        Object.keys(values).length === 0
          ? "No fields changed. Save anyway to refresh auto-password settings?"
          : `Write ${Object.keys(values).length} setting(s) to the local .env file?`,
      okLabel: "Save",
    });
    if (!ok) return;

    const status = $("configStatus");
    status.className = "form-status";
    status.textContent = "Saving…";
    try {
      state.config = await api("/api/config", {
        method: "POST",
        body: JSON.stringify({ values }),
      });
      status.textContent = "Saved.";
      renderConfigForm();
      updateInstallButton();
      setTimeout(closeDrawer, 350);
    } catch (err) {
      status.className = "form-status error";
      status.textContent = err.message || "Could not save.";
    }
  });

  refresh().catch((e) => {
    showAlert({ title: "Installer error", message: e.message || String(e) });
  });
})();
