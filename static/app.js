(() => {
  "use strict";

  const page = document.body.dataset.page;
  const activeJobKey = "xteink-active-job";
  let activeJobId = null;

  const byId = (id) => document.getElementById(id);
  const clear = (node) => { if (node) node.replaceChildren(); };
  const text = (tag, value, className) => {
    const node = document.createElement(tag);
    node.textContent = value == null ? "" : String(value);
    if (className) node.className = className;
    return node;
  };

  function humanDate(value, timeZone, includeTime = true) {
    if (!value) return "Date unavailable";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "Date unavailable";
    const options = { weekday: "long", month: "long", day: "numeric", year: "numeric", timeZone };
    if (includeTime) Object.assign(options, { hour: "numeric", minute: "2-digit" });
    try { return new Intl.DateTimeFormat(undefined, options).format(date); }
    catch (_) { delete options.timeZone; return new Intl.DateTimeFormat(undefined, options).format(date); }
  }

  function editionTitle(edition, timeZone) {
    const date = new Date(edition?.created_at || "");
    if (Number.isNaN(date.getTime())) return edition?.filename || "Saved edition";
    let weekday;
    try { weekday = new Intl.DateTimeFormat(undefined, { weekday: "long", timeZone }).format(date); }
    catch (_) { weekday = new Intl.DateTimeFormat(undefined, { weekday: "long" }).format(date); }
    return `${weekday}’s reading`;
  }

  function deliveryView(edition) {
    const status = edition?.delivery_status || "pending";
    if (status === "uploaded") return { label: "Upload accepted", tone: "complete", detail: "The reader accepted the upload request. Library visibility is not verified." };
    if (status === "queued") return { label: "Waiting for reader", tone: "waiting", detail: "Your edition is retained and ready to retry when the reader is reachable." };
    if (status === "failed") return { label: "Delivery needs attention", tone: "error", detail: edition.delivery_detail || "The upload did not complete." };
    return { label: "Ready to send", tone: "ready", detail: "This edition has not been uploaded yet." };
  }

  function statusLabel(view) {
    return text("span", view.label, `status-label status-${view.tone}`);
  }

  function linkButton(label, href, className = "button button-secondary") {
    const link = text("a", label, className);
    link.href = href;
    return link;
  }

  function actionButton(label, className, handler) {
    const button = text("button", label, className);
    button.type = "button";
    button.addEventListener("click", handler);
    return button;
  }

  function detailsForEdition(edition) {
    const details = document.createElement("details");
    details.className = "edition-details disclosure";
    const summary = text("summary", "Details");
    const content = document.createElement("div");
    const lines = [
      edition.filename ? `File: ${edition.filename}` : "File name unavailable",
      edition.edition_id ? `Edition ID: ${edition.edition_id}` : "Legacy file without edition metadata",
      edition.created_at ? `Created: ${edition.created_at}` : "Creation time unavailable",
    ];
    if (edition.delivery_detail) lines.push(`Delivery detail: ${edition.delivery_detail}`);
    if (edition.warnings?.length) lines.push(`Warnings: ${edition.warnings.join(" · ")}`);
    for (const line of lines) content.append(text("p", line, "muted"));
    details.append(summary, content);
    return details;
  }

  function renderWorkflow(target, kind, heading, detail, logs = []) {
    if (!target) return;
    clear(target);
    const wrap = document.createElement("div");
    wrap.className = "workflow-message";
    if (kind === "working") wrap.append(text("span", "", "progress-dot"));
    else wrap.append(statusLabel({ label: kind === "error" ? "Needs attention" : kind === "waiting" ? "Waiting" : "Complete", tone: kind === "error" ? "error" : kind === "waiting" ? "waiting" : "complete" }));
    const copy = document.createElement("div");
    copy.append(text("strong", heading));
    if (detail) copy.append(text("p", detail));
    if (logs.length) {
      const details = document.createElement("details");
      details.className = "technical-details disclosure";
      details.append(text("summary", "Technical details"), text("pre", logs.join("\n")));
      copy.append(details);
    }
    wrap.append(copy);
    target.append(wrap);
  }

  function outcomeCopy(result) {
    if (result.status === "no_new_articles") return { kind: "complete", heading: "You’re caught up", detail: result.reason || "No new articles were found, so no edition was created." };
    if (result.status === "no_extractable_articles") return { kind: "error", heading: "Nothing readable was available", detail: result.reason || "Articles were found, but their content could not be extracted. No edition was created." };
    if (result.status === "queued") return { kind: "waiting", heading: "Edition ready; reader offline", detail: result.delivery?.reason || "The exact edition was retained for retry. It will not be rebuilt." };
    if (result.status === "complete") return { kind: "complete", heading: "Upload accepted", detail: "The prepared edition was accepted by the reader. Check the reader to confirm it appears in the library." };
    if (["created", "created_with_warnings"].includes(result.status)) return { kind: "complete", heading: "Edition ready", detail: `${result.article_count || 0} articles · about ${result.estimated_reading_minutes || 0} minutes` };
    return { kind: "error", heading: "The operation did not complete", detail: result.reason || "Review the technical details and try again." };
  }

  async function pollJob(jobId, workflow, onComplete) {
    activeJobId = jobId;
    sessionStorage.setItem(activeJobKey, jobId);
    let failures = 0;
    while (activeJobId === jobId) {
      let response;
      let job;
      try {
        response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`);
        job = await response.json();
      } catch (error) {
        failures += 1;
        renderWorkflow(workflow, "working", "Reconnecting to this job", `Progress check ${failures} failed. Your controls will return if recovery is not possible.`);
        if (failures >= 5) {
          renderWorkflow(workflow, "error", "Progress could not be recovered", "The operation may still be running. Refresh to check persisted job state.");
          activeJobId = null; sessionStorage.removeItem(activeJobKey); return;
        }
        await new Promise((resolve) => setTimeout(resolve, 1400));
        continue;
      }
      if (!response.ok) {
        renderWorkflow(workflow, "error", "This job is no longer available", job.reason || "It may have expired. You can safely start again.");
        activeJobId = null; sessionStorage.removeItem(activeJobKey); return;
      }
      const stageLabels = {
        starting: "Starting…",
        fetching_and_building: "Fetching feeds, extracting articles, and building the EPUB…",
        fetching_and_extracting: "Fetching feeds and extracting articles…",
        sending: "Sending the prepared edition…",
      };
      if (job.status === "running") {
        renderWorkflow(workflow, "working", stageLabels[job.stage] || "Working…", "This may take a few minutes.", job.logs || []);
        await new Promise((resolve) => setTimeout(resolve, 900));
        continue;
      }
      activeJobId = null;
      sessionStorage.removeItem(activeJobKey);
      if (job.status === "interrupted") {
        renderWorkflow(workflow, "error", "This job was interrupted", "The server restarted before the operation finished. No successful result is being assumed.", job.logs || []);
        return;
      }
      const result = job.result || { status: "failed", reason: "No result was returned." };
      const copy = outcomeCopy(result);
      renderWorkflow(workflow, job.status === "failed" ? "error" : copy.kind, copy.heading, copy.detail, job.logs || []);
      if (job.status !== "failed" && onComplete) await onComplete(result);
      return;
    }
  }

  async function startEditionSend(edition, workflow) {
    renderWorkflow(workflow, "working", edition.delivery_status === "queued" ? "Retrying delivery…" : "Sending to Xteink…", "The existing edition will be sent without rebuilding.");
    const response = await fetch("/api/editions/send", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ edition_id: edition.edition_id }) });
    const accepted = await response.json();
    if (!response.ok || !accepted.job_id) {
      renderWorkflow(workflow, "error", "Delivery could not start", accepted.reason || "No job was created.");
      return;
    }
    await pollJob(accepted.job_id, workflow, async () => { await refreshVisibleData(); });
  }

  async function sendLegacyFile(edition, workflow) {
    renderWorkflow(workflow, "working", "Sending to Xteink…", "This saved file will be sent without rebuilding.");
    let response;
    let result;
    try {
      response = await fetch("/api/files/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: edition.filename }),
      });
      result = await response.json();
    } catch (_) {
      renderWorkflow(workflow, "error", "Delivery could not be checked", "The request did not complete. The local copy is still available.");
      return;
    }
    if (!response.ok) {
      const waiting = result.status === "unreachable";
      renderWorkflow(workflow, waiting ? "waiting" : "error", waiting ? "Reader is not reachable" : "Delivery did not complete", result.reason || "The local copy is still available.");
      return;
    }
    renderWorkflow(workflow, "complete", "Upload accepted", "The reader accepted the upload request. Check the reader to confirm library visibility.");
    await refreshVisibleData();
  }

  function editionRow(edition, timeZone, workflow, compact = false) {
    const row = document.createElement("article");
    row.className = "edition-row";
    const main = document.createElement("div"); main.className = "edition-main";
    main.append(text("h3", editionTitle(edition, timeZone)));
    const meta = document.createElement("div"); meta.className = "edition-meta";
    meta.append(text("span", humanDate(edition.created_at, timeZone, false)));
    if (edition.article_count != null) meta.append(text("span", `${edition.article_count} articles`));
    if (edition.estimated_reading_minutes != null) meta.append(text("span", `About ${edition.estimated_reading_minutes} min`));
    meta.append(statusLabel(deliveryView(edition)));
    main.append(meta);
    const actions = document.createElement("div"); actions.className = "edition-row-actions";
    const delivery = edition.delivery_status || "pending";
    if (edition.edition_id && delivery !== "uploaded" && edition.local_copy_exists !== false) {
      actions.append(actionButton(delivery === "queued" || delivery === "failed" ? "Retry delivery" : "Send", "text-button", () => startEditionSend(edition, workflow)));
    }
    if (edition.edition_id && edition.local_copy_exists !== false) actions.append(linkButton("Download", `/api/editions/download?edition_id=${encodeURIComponent(edition.edition_id)}`, "text-button"));
    if (!edition.edition_id && edition.filename && edition.local_copy_exists !== false) {
      if (delivery !== "uploaded") actions.append(actionButton("Send", "text-button", () => sendLegacyFile(edition, workflow)));
      actions.append(linkButton("Download", `/api/files/download?filename=${encodeURIComponent(edition.filename)}`, "text-button"));
    }
    if (!compact && edition.filename && edition.local_copy_exists !== false) {
      actions.append(actionButton("Delete local copy", "text-button danger-text", () => deleteLocalEdition(edition, workflow)));
    }
    if (delivery === "uploaded" && edition.edition_id && !compact) {
      const resend = document.createElement("details"); resend.className = "disclosure";
      resend.append(text("summary", "More actions"), actionButton("Resend intentionally", "text-button", () => startEditionSend(edition, workflow)));
      actions.append(resend);
    }
    if (delivery === "uploaded" && !edition.edition_id && edition.filename && !compact) {
      const resend = document.createElement("details"); resend.className = "disclosure";
      resend.append(text("summary", "More actions"), actionButton("Resend intentionally", "text-button", () => sendLegacyFile(edition, workflow)));
      actions.append(resend);
    }
    row.append(main, actions);
    if (!compact) row.append(detailsForEdition(edition));
    return row;
  }

  async function deleteLocalEdition(edition, workflow) {
    if (!window.confirm(`Delete only the local copy of ${edition.filename}? Edition history will be preserved and the file will move to recoverable trash.`)) return;
    const response = await fetch("/api/files/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ filename: edition.filename }) });
    const result = await response.json();
    renderWorkflow(workflow, response.ok ? "complete" : "error", response.ok ? "Local copy moved to trash" : "Local copy was not deleted", response.ok ? "Edition history remains available." : result.reason);
    await refreshVisibleData();
  }

  function renderLatest(payload) {
    const target = byId("latestEdition");
    if (!target) return;
    clear(target); target.setAttribute("aria-busy", "false");
    const latest = payload.latest_edition;
    const timeZone = payload.effective_schedule?.timezone;
    const copy = document.createElement("div"); copy.className = "latest-copy";
    const actions = document.createElement("div"); actions.className = "latest-actions";
    const workflow = document.createElement("div"); workflow.id = "todayWorkflow"; workflow.className = "workflow-region"; workflow.setAttribute("role", "status"); workflow.setAttribute("aria-live", "polite");
    if (!latest) {
      copy.append(text("p", "Nothing prepared yet", "eyebrow"), text("h2", payload.subscription_count ? "Make today’s reading" : "Begin with a few sources"), text("p", payload.subscription_count ? "Your sources are ready. Prepare an edition when you want fresh reading." : "Add one or two sources, then return here to prepare your first edition."));
      actions.append(linkButton(payload.subscription_count ? "Prepare reading" : "Add sources", payload.subscription_count ? "/editions" : "/sources", "button button-primary"));
      if (payload.subscription_count) actions.append(linkButton("Prepare & send automatically", "/editions?mode=prepare-send", "button button-secondary"));
    } else {
      const view = deliveryView(latest);
      copy.append(statusLabel(view), text("h2", editionTitle(latest, timeZone)), text("p", humanDate(latest.created_at, timeZone)));
      const meta = document.createElement("div"); meta.className = "edition-meta";
      meta.append(text("span", `${latest.article_count ?? "Unknown"} articles`), text("span", latest.estimated_reading_minutes != null ? `About ${latest.estimated_reading_minutes} minutes` : "Reading time unavailable"));
      copy.append(meta, text("p", view.detail));
      if (latest.local_copy_exists === false) {
        actions.append(statusLabel({ label: "Local copy missing", tone: "error" }), linkButton("Prepare new reading", "/editions", "button button-primary"));
      } else if (latest.delivery_status === "uploaded") {
        actions.append(statusLabel({ label: "Upload accepted", tone: "complete" }), linkButton("Prepare new reading", "/editions", "button button-primary"));
      } else {
        const label = ["queued", "failed"].includes(latest.delivery_status) ? "Retry delivery" : "Send to Xteink";
        actions.append(actionButton(label, "button button-primary", () => startEditionSend(latest, workflow)));
      }
      const secondary = document.createElement("div"); secondary.className = "secondary-links";
      secondary.append(linkButton("Download", `/api/editions/download?edition_id=${encodeURIComponent(latest.edition_id)}`, "text-button"), linkButton("View details", "/editions", "text-button"));
      actions.append(secondary);
    }
    copy.append(workflow);
    target.append(copy, actions);
  }

  function renderRecent(payload) {
    const target = byId("recentEditions"); if (!target) return; clear(target);
    const editions = (payload.recent_editions || []).slice(1, 4);
    if (!editions.length) { target.append(text("p", "Your recent editions will collect here.", "empty-state")); return; }
    for (const edition of editions) target.append(editionRow(edition, payload.effective_schedule?.timezone, byId("todayWorkflow"), true));
  }

  async function loadToday() {
    const response = await fetch("/api/dashboard"); const payload = await response.json();
    const timeZone = payload.effective_schedule?.timezone;
    byId("friendlyDate").textContent = humanDate(new Date().toISOString(), timeZone, false);
    const device = payload.device_check || {};
    byId("deviceStatus").textContent = device.status === "reachable" ? `Reachable at ${device.host}` : device.status === "unreachable" ? `Last check: unreachable at ${device.host}` : "Not checked yet";
    const schedule = payload.effective_schedule || {};
    byId("scheduleStatus").textContent = schedule.generation_time ? `${schedule.generation_time}–${schedule.delivery_window_end} · ${schedule.timezone}` : "No schedule available";
    renderLatest(payload); renderRecent(payload);
    const active = (payload.active_jobs || [])[0];
    if (active && active.job_id) pollJob(active.job_id, byId("todayWorkflow"), refreshVisibleData);
  }

  async function loadEditions() {
    const [dashboardResponse, filesResponse] = await Promise.all([fetch("/api/dashboard"), fetch("/api/files")]);
    const dashboard = await dashboardResponse.json(); const files = await filesResponse.json();
    const target = byId("editionCollection"); clear(target);
    const knownNames = new Set((dashboard.recent_editions || []).map((item) => item.filename));
    const legacy = (files.files || []).filter((item) => !knownNames.has(item.name)).map((item) => ({ filename: item.name, local_copy_exists: true, delivery_status: item.uploaded ? "uploaded" : "pending", delivery_detail: item.error, size_bytes: item.size_bytes }));
    const editions = [...(dashboard.recent_editions || []), ...legacy];
    if (!editions.length) { target.append(text("p", "No editions yet. Choose a size above to prepare your first one.", "empty-state")); return; }
    for (const edition of editions) target.append(editionRow(edition, dashboard.effective_schedule?.timezone, byId("editionWorkflow")));
  }

  async function refreshVisibleData() {
    if (page === "today") await loadToday();
    if (page === "editions") await loadEditions();
  }

  async function startGeneration(mode, form) {
    const workflow = byId("editionWorkflow");
    const preset = new FormData(form).get("preset") || "full";
    const payload = {
      days_back: Number(byId("days_back").value), preset,
      include_previous: byId("includePrevious").checked,
      backfill_to_last_generated: byId("backfillToLastGenerated").checked,
    };
    const endpoint = mode === "prepare-send" ? "/api/prepare-and-send" : "/api/epubs/generate";
    renderWorkflow(workflow, "working", mode === "prepare-send" ? "Preparing, then sending automatically…" : "Preparing your edition…", "Fetching feeds and looking for readable articles.");
    const response = await fetch(endpoint, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    const accepted = await response.json();
    if (!response.ok || !accepted.job_id) { renderWorkflow(workflow, "error", "Preparation could not start", accepted.reason || "No job was created."); return; }
    await pollJob(accepted.job_id, workflow, async (result) => {
      // No-content outcomes arrive without a nested generation object. Never imply success.
      if (["no_new_articles", "no_extractable_articles"].includes(result.status)) return;
      await loadEditions();
    });
  }

  function renderValidationRows(results) {
    const body = byId("resultsBody"); if (!body) return; clear(body);
    for (const result of results) {
      const row = document.createElement("tr");
      const values = [result.feed, result["1 day"], result["7 days"], result["30 days"], result.status === "ok" ? "Healthy" : `Needs attention: ${result.reason || "unknown"}`];
      for (const value of values) row.append(text("td", value));
      body.append(row);
    }
  }

  async function startSimpleJob(endpoint, action, workflow, onComplete) {
    renderWorkflow(workflow, "working", action, "This may take a moment.");
    const response = await fetch(endpoint, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ auto_discover_invalid_feeds: byId("autoDiscoverInvalidFeeds")?.checked }) });
    const accepted = await response.json();
    if (!response.ok || !accepted.job_id) { renderWorkflow(workflow, "error", `${action} could not start`, accepted.reason); return; }
    await pollJob(accepted.job_id, workflow, onComplete);
  }

  async function loadSettings() {
    const response = await fetch("/api/settings"); const settings = await response.json(); const form = byId("settingsForm");
    for (const key of ["device_address", "generation_time", "delivery_window_end", "timezone"]) if (form.elements[key]) form.elements[key].value = settings[key] || "";
    const overrides = settings.overridden_by_environment || {};
    for (const [key, envName] of Object.entries(overrides)) {
      if (form.elements[key]) { form.elements[key].disabled = true; form.elements[key].setAttribute("aria-disabled", "true"); }
      if (key === "device_address") byId("deviceOverride").textContent = `Managed by ${envName}; change it in the server environment.`;
    }
    const scheduleNotes = Object.entries(overrides).filter(([key]) => ["generation_time", "delivery_window_end", "timezone"].includes(key)).map(([key, env]) => `${key.replaceAll("_", " ")} is managed by ${env}`);
    byId("scheduleOverrides").textContent = scheduleNotes.join(". ");
  }

  function wireCommonActions() {
    for (const form of document.querySelectorAll('form[action="/feeds/delete"]')) form.addEventListener("submit", (event) => { if (!window.confirm("Remove this source? You can add it again later.")) event.preventDefault(); });
    byId("generateForm")?.addEventListener("submit", (event) => { event.preventDefault(); startGeneration(event.submitter?.dataset.mode || "prepare", event.currentTarget); });
    byId("validateBtn")?.addEventListener("click", () => startSimpleJob("/api/validate", "Checking source health…", byId("todayWorkflow"), renderValidationRows));
    byId("uploadBtn")?.addEventListener("click", () => startSimpleJob("/api/upload-pending", "Retrying pending uploads…", byId("todayWorkflow"), refreshVisibleData));
    byId("deleteUploadedBtn")?.addEventListener("click", async () => {
      const workflow = byId("editionWorkflow"); const preview = await (await fetch("/api/files/cleanup-preview")).json();
      if (!preview.files?.length) { renderWorkflow(workflow, "complete", "Nothing to clean up", `Newest local copy preserved: ${preview.preserved_newest || "none"}.`); return; }
      if (!window.confirm(`Move these delivered local copies to recoverable trash?\n\n${preview.files.join("\n")}\n\nNewest preserved: ${preview.preserved_newest}`)) return;
      const response = await fetch("/api/files/delete-uploaded", { method: "POST" }); const result = await response.json();
      renderWorkflow(workflow, result.failed_count ? "error" : "complete", `Moved ${result.deleted_count || 0} local copies to trash`, result.failed_count ? `${result.failed_count} could not be moved.` : "Edition history is preserved."); await loadEditions();
    });
    byId("settingsForm")?.addEventListener("submit", async (event) => {
      event.preventDefault(); const values = Object.fromEntries(new FormData(event.currentTarget));
      const response = await fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(values) }); const result = await response.json();
      byId("settingsResult").textContent = response.ok ? "Settings saved." : result.reason || "Settings were not saved.";
      if (response.ok) await loadSettings();
    });
    byId("testConnectionBtn")?.addEventListener("click", async () => {
      const resultNode = byId("connectionResult"); resultNode.textContent = "Testing network connection…";
      const response = await fetch("/api/device/test", { method: "POST" }); const result = await response.json();
      resultNode.textContent = response.ok ? `Connection succeeded at ${result.host}. No book was uploaded.` : `Could not reach ${result.host}. Check the address, network, and transfer mode.`;
    });
  }

  async function init() {
    wireCommonActions();
    if (page === "today") await loadToday();
    if (page === "editions") await loadEditions();
    if (page === "settings") await loadSettings();
    const remembered = sessionStorage.getItem(activeJobKey);
    const workflow = page === "today" ? byId("todayWorkflow") : byId("editionWorkflow");
    if (remembered && workflow && remembered !== activeJobId) pollJob(remembered, workflow, refreshVisibleData);
  }

  init().catch((error) => {
    const workflow = byId("todayWorkflow") || byId("editionWorkflow") || byId("settingsResult");
    if (workflow?.classList.contains("workflow-region")) renderWorkflow(workflow, "error", "This page could not finish loading", error.message);
    else if (workflow) workflow.textContent = `This page could not finish loading: ${error.message}`;
  });
})();
