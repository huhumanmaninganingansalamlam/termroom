(() => {
  "use strict";

  const messages = window.TermroomI18n || {};
  const tr = (key) => messages[key] || key;

  const retentionFormatter = new Intl.DateTimeFormat(document.documentElement.lang || undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short",
  });
  document.querySelectorAll("[data-run-retention]").forEach((element) => {
    const expiresAt = Date.parse(element.dataset.expiresAt || "");
    if (!Number.isFinite(expiresAt)) return;
    const template = element.dataset.template || "{time}";
    element.textContent = template.replace("{time}", retentionFormatter.format(expiresAt));
  });

  const createUuid = () => {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID();
    if (!window.crypto?.getRandomValues) return "";
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = [...bytes].map((value) => value.toString(16).padStart(2, "0"));
    return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
  };

  const form = document.querySelector("#remote-run-form");
  if (form) {
    const sourceInputs = [...form.querySelectorAll("input[name='source_kind']")];
    const sourcePanels = [...form.querySelectorAll("[data-source-panel]")];
    const sourceControlInitialDisabled = new WeakMap();
    const sourcePanelControlSelector = "button, fieldset, input, optgroup, option, select, textarea";
    const errorBox = form.querySelector("#remote-run-form-error");
    const submit = form.querySelector("button[type='submit']");
    const progressBox = form.querySelector("#remote-run-upload-progress");
    const progress = progressBox?.querySelector("progress");
    let pendingZipRun = null;

    const selectedKind = () => sourceInputs.find((input) => input.checked)?.value || "workspace";
    const updatePanels = () => {
      const kind = selectedKind();
      sourcePanels.forEach((panel) => {
        const active = panel.dataset.sourcePanel === kind;
        panel.hidden = !active;
        panel.querySelectorAll(sourcePanelControlSelector).forEach((control) => {
          if (!sourceControlInitialDisabled.has(control)) {
            sourceControlInitialDisabled.set(control, control.disabled);
          }
          control.disabled = !active || sourceControlInitialDisabled.get(control);
        });
      });
    };
    sourceInputs.forEach((input) => input.addEventListener("change", updatePanels));
    updatePanels();

    const showError = (message) => {
      if (!errorBox) return;
      errorBox.textContent = message;
      errorBox.hidden = false;
      errorBox.scrollIntoView({ block: "nearest" });
    };

    const clearError = () => {
      if (errorBox) errorBox.hidden = true;
    };
    form.addEventListener("input", clearError);
    form.addEventListener("change", clearError);

    const uploadArchive = (runId, file) => new Promise((resolve, reject) => {
      const request = new XMLHttpRequest();
      const filename = encodeURIComponent(file.name);
      request.open("POST", `${form.dataset.uploadPrefix}/${runId}/archive?filename=${filename}`);
      request.setRequestHeader("X-Termroom-CSRF", form.dataset.csrf || "");
      request.setRequestHeader("Content-Type", "application/zip");
      request.responseType = "json";
      request.upload.addEventListener("progress", (event) => {
        if (!event.lengthComputable || !progress) return;
        progress.value = Math.round((event.loaded / event.total) * 100);
      });
      request.addEventListener("load", () => {
        const body = request.response || {};
        if (request.status >= 200 && request.status < 300 && body.ok !== false) {
          resolve(body);
          return;
        }
        reject(new Error(body.error || tr("remote_run.error.upload_network")));
      });
      request.addEventListener("error", () => reject(new Error(tr("remote_run.error.upload_network"))));
      request.addEventListener("abort", () => reject(new Error(tr("remote_run.error.upload_aborted"))));
      request.send(file);
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (errorBox) errorBox.hidden = true;
      const data = new FormData(form);
      const sourceKind = selectedKind();
      const payload = {
        source_kind: sourceKind,
        target_computer_id: String(data.get("target_computer_id") || ""),
        command: String(data.get("command") || ""),
      };
      let archive = null;
      if (sourceKind === "workspace") {
        payload.source_workspace_id = String(data.get("source_workspace_id") || "");
        payload.source_path = String(data.get("source_path") || ".");
      } else if (sourceKind === "git") {
        payload.source_url = String(data.get("source_url") || "");
      } else {
        archive = form.querySelector("input[name='archive']")?.files?.[0] || null;
        payload.archive_name = archive?.name || "";
      }
      const fingerprint = JSON.stringify(payload);
      const runId = (
        sourceKind === "zip" && pendingZipRun?.fingerprint === fingerprint
          ? pendingZipRun.id
          : createUuid()
      );
      if (!runId) {
        showError(tr("remote_run.error.uuid_unavailable"));
        return;
      }
      payload.id = runId;

      submit.disabled = true;
      form.setAttribute("aria-busy", "true");
      try {
        const response = await fetch(form.dataset.createUrl, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Termroom-CSRF": form.dataset.csrf || "",
          },
          body: JSON.stringify(payload),
        });
        const result = await response.json();
        if (!response.ok || result.ok === false) {
          throw new Error(result.error || tr("remote_run.error.start_failed"));
        }
        if (sourceKind === "zip") {
          pendingZipRun = { id: runId, fingerprint };
          if (!archive) throw new Error(tr("remote_run.error.zip_required"));
          if (progressBox) progressBox.hidden = false;
          await uploadArchive(runId, archive);
        }
        window.location.assign(result.detail_url || `/remote-runs/${runId}`);
      } catch (error) {
        showError(error?.message || String(error));
        submit.disabled = false;
        form.removeAttribute("aria-busy");
        if (progressBox) progressBox.hidden = true;
      }
    });
  }

  document.querySelectorAll("[data-run-force-stop]").forEach((forceStopForm) => {
    const requestedAt = Date.parse(forceStopForm.dataset.stopRequestedAt || "");
    const remaining = Number.isFinite(requestedAt)
      ? Math.max(0, 4000 - (Date.now() - requestedAt))
      : 4000;
    window.setTimeout(() => { forceStopForm.hidden = false; }, remaining);
  });

  const wait = document.querySelector("#remote-run-wait");
  if (wait && ["preparing", "running"].includes(wait.dataset.state)) {
    const stateLabel = wait.querySelector("[data-run-state]");
    const errorBox = wait.querySelector("[data-run-error]");
    const connectionNotice = wait.querySelector("[data-run-connection]");
    let stopped = false;
    const poll = async () => {
      if (stopped || document.hidden) return;
      try {
        const response = await fetch(wait.dataset.statusUrl, { headers: { Accept: "application/json" } });
        const result = await response.json();
        if (!response.ok || result.ok === false) throw new Error(result.error || "Remote Run status failed");
        if (errorBox) errorBox.hidden = true;
        if (connectionNotice) connectionNotice.hidden = result.connection !== "offline";
        if (result.workspace_url) {
          stopped = true;
          window.location.replace(result.workspace_url);
          return;
        }
        if (["preparing", "running"].includes(result.state)) {
          const stateKey = result.state === "preparing"
            ? `remote_run.phase.${result.phase || "scanning"}`
            : "remote_run.state.running";
          if (stateLabel) stateLabel.textContent = tr(stateKey);
          return;
        }
        stopped = true;
        window.location.reload();
      } catch (error) {
        if (errorBox) {
          errorBox.textContent = error?.message || String(error);
          errorBox.hidden = false;
        }
      }
    };
    window.setInterval(poll, 1500);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) poll();
    });
    poll();
  }

  const workspaceRun = document.querySelector("[data-remote-run-workspace]");
  if (workspaceRun && ["preparing", "running"].includes(workspaceRun.dataset.state)) {
    const stateLabel = workspaceRun.querySelector("[data-run-workspace-state]");
    const stateChip = workspaceRun.querySelector(".state-chip");
    let stopped = false;
    const poll = async () => {
      if (stopped || document.hidden) return;
      try {
        const response = await fetch(workspaceRun.dataset.statusUrl, {
          cache: "no-store",
          headers: { Accept: "application/json" },
        });
        const result = await response.json();
        if (!response.ok || result.ok === false) {
          throw new Error(result.error || "Remote Run status failed");
        }
        if (!["preparing", "running"].includes(result.state)) {
          stopped = true;
          window.location.reload();
          return;
        }
        workspaceRun.dataset.state = result.state;
        stateChip?.classList.toggle("running", result.state === "running");
        if (stateLabel) {
          stateLabel.textContent = tr(`remote_run.state.${result.state}`);
        }
      } catch (_error) {
        // A dropped SSH connection does not change the Run state. Keep polling;
        // the existing terminal reconnect UI remains the visible connection signal.
      }
    };
    window.setInterval(poll, 1500);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) poll();
    });
    poll();
  }

  const recentRuns = [...document.querySelectorAll("[data-remote-run-recent]")]
    .filter((row) => ["preparing", "running"].includes(row.dataset.runState));
  if (recentRuns.length) {
    let reloading = false;
    const pollRecentRuns = async () => {
      if (reloading || document.hidden) return;
      const results = await Promise.allSettled(recentRuns.map(async (row) => {
        const response = await fetch(row.dataset.statusUrl, {
          cache: "no-store",
          headers: { Accept: "application/json" },
        });
        const result = await response.json();
        if (!response.ok || result.ok === false) {
          throw new Error(result.error || "Remote Run status failed");
        }
        return result;
      }));
      if (results.some((result) => (
        result.status === "fulfilled"
        && !["preparing", "running"].includes(result.value.state)
      ))) {
        reloading = true;
        window.location.reload();
      }
    };
    window.setInterval(pollRecentRuns, 3000);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) pollRecentRuns();
    });
    pollRecentRuns();
  }
})();
