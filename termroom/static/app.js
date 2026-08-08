(() => {
  const messages = window.TermroomI18n || {};
  const tr = (key, values = {}) => {
    let text = messages[key] || key;
    Object.entries(values).forEach(([name, value]) => {
      text = text.replaceAll(`{${name}}`, String(value));
    });
    return text;
  };
  window.TermroomT = tr;

  if ("serviceWorker" in navigator && window.isSecureContext) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }

  const setViewportHeight = () => {
    const height = window.visualViewport?.height || window.innerHeight;
    document.documentElement.style.setProperty("--app-height", `${height}px`);
  };
  setViewportHeight();
  window.visualViewport?.addEventListener("resize", setViewportHeight);
  window.addEventListener("resize", setViewportHeight);

  document.querySelectorAll("[data-language-select]").forEach((select) => {
    select.addEventListener("change", () => {
      const next = `${window.location.pathname}${window.location.search}${window.location.hash}`;
      window.location.assign(
        `/locale/${encodeURIComponent(select.value)}?next=${encodeURIComponent(next)}`,
      );
    });
  });

  document.addEventListener("submit", (event) => {
    const message = event.submitter?.dataset?.confirm || event.target.dataset?.confirm;
    if (message && !window.confirm(message)) event.preventDefault();
  });

  document.addEventListener("click", async (event) => {
    const fileActionButton = event.target.closest("[data-file-actions]");
    if (fileActionButton) {
      const dialog = document.querySelector("#file-actions-dialog");
      if (dialog instanceof HTMLDialogElement) {
        const name = fileActionButton.dataset.fileName || "";
        const path = fileActionButton.dataset.filePath || "";
        dialog.querySelector("#file-actions-title").textContent = name;
        dialog.querySelector("#file-rename-path").value = path;
        dialog.querySelector("#file-rename-name").value = name;
        dialog.querySelector("#file-delete-path").value = path;
        dialog.querySelector("#file-delete-form").dataset.confirm =
          tr("js.delete_confirm", { name });
        const download = dialog.querySelector("#file-download-action");
        const isDirectory = fileActionButton.dataset.fileDirectory === "1";
        if (download) {
          const encodedPath = encodeURIComponent(path).replaceAll("%2F", "/");
          download.hidden = false;
          download.href = isDirectory
            ? `/w/${dialog.dataset.workspaceId}/archive/${encodedPath}`
            : `/w/${dialog.dataset.workspaceId}/download/${encodedPath}`;
        }
        dialog.showModal();
        window.setTimeout(() => dialog.querySelector("#file-rename-name")?.focus(), 0);
      }
    }

    const detailsButton = event.target.closest("[data-open-details]");
    if (detailsButton) {
      const details = document.querySelector(detailsButton.dataset.openDetails);
      if (details instanceof HTMLDetailsElement) {
        details.open = true;
        const input = details.querySelector("input:not([type='hidden'])");
        window.setTimeout(() => input?.focus(), 0);
      }
    }

    const copyButton = event.target.closest("[data-copy-target]");
    if (copyButton) {
      const target = document.querySelector(copyButton.dataset.copyTarget);
      if (!target) return;
      try {
        await navigator.clipboard.writeText(target.textContent || "");
        const previous = copyButton.textContent;
        copyButton.textContent = tr("common.copied");
        window.setTimeout(() => (copyButton.textContent = previous), 1000);
      } catch {
        const fallback = document.createElement("textarea");
        fallback.value = target.textContent || "";
        fallback.setAttribute("readonly", "");
        fallback.style.position = "fixed";
        fallback.style.opacity = "0";
        document.body.append(fallback);
        fallback.select();
        document.execCommand("copy");
        fallback.remove();
      }
    }

    const closeButton = event.target.closest("[data-close-dialog]");
    if (closeButton) closeButton.closest("dialog")?.close();
  });

  const fileSelectionBar = document.querySelector("#file-bulk-form");
  const fileSelectionCount = document.querySelector("#file-selection-count");
  const fileSelectionClear = document.querySelector("#file-selection-clear");
  const fileSelectionInputs = () => [...document.querySelectorAll("[data-file-select]")];

  const syncFileSelection = () => {
    if (!fileSelectionBar) return;
    const inputs = fileSelectionInputs();
    const selected = inputs.filter((input) => input.checked);
    fileSelectionBar.hidden = selected.length === 0;
    if (fileSelectionCount) fileSelectionCount.textContent = String(selected.length);
    inputs.forEach((input) => {
      input.closest(".file-row-with-actions")?.classList.toggle("is-selected", input.checked);
    });
    const fileSelectAll = document.querySelector("#file-select-all");
    if (fileSelectAll) {
      fileSelectAll.checked = inputs.length > 0 && selected.length === inputs.length;
      fileSelectAll.indeterminate = selected.length > 0 && selected.length < inputs.length;
    }
  };

  document.addEventListener("change", (event) => {
    if (event.target.matches("[data-file-select]")) {
      syncFileSelection();
      return;
    }
    if (event.target.matches("#file-select-all")) {
      fileSelectionInputs().forEach((input) => {
        input.checked = event.target.checked;
      });
      syncFileSelection();
    }
  });
  fileSelectionClear?.addEventListener("click", () => {
    fileSelectionInputs().forEach((input) => {
      input.checked = false;
    });
    syncFileSelection();
  });
  syncFileSelection();

  const liveSearchForm = document.querySelector("[data-live-file-search]");
  const liveSearchInput = liveSearchForm?.querySelector("#file-search-input");
  const liveSearchClear = liveSearchForm?.querySelector("#file-search-clear");
  const liveSearchResults = document.querySelector("#file-results");
  let liveSearchTimer = 0;
  let liveSearchRequest = null;

  const runLiveFileSearch = async () => {
    if (!liveSearchForm || !liveSearchInput || !liveSearchResults) return;
    liveSearchRequest?.abort();
    liveSearchRequest = new AbortController();
    const params = new URLSearchParams(new FormData(liveSearchForm));
    if (!params.get("q")) params.delete("q");
    params.delete("page");
    const url = `${liveSearchForm.action}${params.size ? `?${params}` : ""}`;
    liveSearchResults.setAttribute("aria-busy", "true");
    try {
      const response = await fetch(url, {
        credentials: "same-origin",
        headers: { "X-Termroom-Partial": "file-results" },
        signal: liveSearchRequest.signal,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      liveSearchResults.innerHTML = await response.text();
      history.replaceState({}, "", url);
      if (liveSearchClear) liveSearchClear.hidden = !liveSearchInput.value.trim();
      syncFileSelection();
    } catch (error) {
      if (error?.name !== "AbortError") {
        window.location.assign(url);
      }
    } finally {
      liveSearchResults.setAttribute("aria-busy", "false");
    }
  };

  const scheduleLiveFileSearch = () => {
    window.clearTimeout(liveSearchTimer);
    liveSearchTimer = window.setTimeout(runLiveFileSearch, 180);
  };

  liveSearchInput?.addEventListener("input", scheduleLiveFileSearch);
  liveSearchForm?.addEventListener("submit", (event) => {
    event.preventDefault();
    window.clearTimeout(liveSearchTimer);
    runLiveFileSearch();
  });
  liveSearchClear?.addEventListener("click", () => {
    liveSearchInput.value = "";
    liveSearchInput.focus({ preventScroll: true });
    runLiveFileSearch();
  });

  const uploadInput = document.querySelector("#file-upload-input");
  const uploadForm = document.querySelector("#file-upload-form");
  const uploadCheckUrl = uploadForm?.dataset.checkUrl || "";
  const uploadDialog = document.querySelector("#upload-confirm-dialog");
  const uploadConflictList = document.querySelector("#upload-conflict-list");
  const overwriteInput = document.querySelector("#upload-overwrite");
  const formatBytes = (bytes) => {
    if (bytes < 1024) return `${bytes} B`;
    const units = ["KB", "MB", "GB", "TB"];
    let value = bytes / 1024;
    for (const unit of units) {
      if (value < 1024 || unit === units.at(-1)) {
        return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${unit}`;
      }
      value /= 1024;
    }
    return `${bytes} B`;
  };

  const showUploadCheckError = (message) => {
    if (uploadProgressPanel) uploadProgressPanel.hidden = false;
    if (uploadProgressBar) uploadProgressBar.value = 0;
    if (uploadCancelButton) uploadCancelButton.disabled = true;
    if (uploadRefreshLink) uploadRefreshLink.hidden = false;
    if (uploadProgressStatus) uploadProgressStatus.textContent = message;
    if (uploadInput) uploadInput.value = "";
    if (overwriteInput) overwriteInput.value = "0";
  };

  uploadInput?.addEventListener("change", async () => {
    if (!uploadForm || !uploadInput.files?.length) return;
    overwriteInput.value = "0";
    const selectedFiles = [...uploadInput.files];
    const fileByName = new Map(selectedFiles.map((file) => [file.name, file]));
    const csrf = uploadForm.querySelector('input[name="_csrf"]')?.value || "";
    const parent = uploadForm.querySelector('input[name="parent"]')?.value || ".";
    if (!uploadCheckUrl || !csrf) {
      uploadForm.requestSubmit();
      return;
    }
    if (uploadProgressPanel) uploadProgressPanel.hidden = false;
    if (uploadRefreshLink) uploadRefreshLink.hidden = true;
    if (uploadCancelButton) uploadCancelButton.disabled = true;
    if (uploadProgressStatus) uploadProgressStatus.textContent = tr("files.upload_checking");
    let result;
    try {
      const response = await fetch(uploadCheckUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-Termroom-CSRF": csrf,
        },
        body: JSON.stringify({ parent, names: selectedFiles.map((file) => file.name) }),
      });
      if (response.status === 401) throw new Error(tr("files.upload_auth_required"));
      if (response.status === 403) throw new Error(tr("files.upload_refresh_required"));
      result = await response.json();
      if (!response.ok || !result.ok) {
        throw new Error(result.error || `HTTP ${response.status}`);
      }
    } catch (error) {
      showUploadCheckError(tr("files.upload_failed", { error: error?.message || String(error) }));
      return;
    }
    const conflicts = (result.conflicts || [])
      .map((current) => ({ file: fileByName.get(current.name), current }))
      .filter(({ file }) => Boolean(file));

    if (!conflicts.length) {
      if (uploadProgressPanel) uploadProgressPanel.hidden = true;
      uploadForm.requestSubmit();
      return;
    }
    if (!(uploadDialog instanceof HTMLDialogElement) || !uploadConflictList) return;
    uploadConflictList.replaceChildren(
      ...conflicts.map(({ file, current }) => {
        const row = document.createElement("article");
        const title = document.createElement("strong");
        title.textContent = file.name;
        const currentLine = document.createElement("span");
        currentLine.textContent = tr("files.current_file", {
          size: formatBytes(current.size),
          mtime: current.mtime,
        });
        const nextLine = document.createElement("span");
        nextLine.textContent = tr("files.new_file", { size: formatBytes(file.size) });
        row.append(title, currentLine, nextLine);
        return row;
      }),
    );
    if (uploadProgressPanel) uploadProgressPanel.hidden = true;
    uploadDialog.showModal();
  });

  document.querySelector("#confirm-upload-overwrite")?.addEventListener("click", () => {
    if (!uploadForm) return;
    overwriteInput.value = "1";
    uploadDialog?.close();
    uploadForm.requestSubmit();
  });

  uploadDialog?.addEventListener("close", () => {
    if (overwriteInput?.value !== "1" && uploadInput) uploadInput.value = "";
  });

  const uploadProgressPanel = document.querySelector("#upload-progress-panel");
  const uploadProgressBar = document.querySelector("#upload-progress-bar");
  const uploadProgressStatus = document.querySelector("#upload-progress-status");
  const uploadCancelButton = document.querySelector("#cancel-file-upload");
  const uploadRefreshLink = document.querySelector("#upload-refresh-link");
  let activeUploadRequest = null;

  const uploadOneFile = ({ file, index, total, completedBytes, totalBytes, overwrite }) =>
    new Promise((resolve, reject) => {
      const streamUrl = uploadForm?.dataset.streamUrl;
      const csrf = uploadForm?.querySelector('input[name="_csrf"]')?.value || "";
      const parent = uploadForm?.querySelector('input[name="parent"]')?.value || ".";
      if (!streamUrl || !csrf) {
        reject(new Error("Streaming upload is unavailable"));
        return;
      }
      const query = new URLSearchParams({
        parent,
        filename: file.name,
        overwrite: overwrite ? "true" : "false",
      });
      const xhr = new XMLHttpRequest();
      activeUploadRequest = xhr;
      if (uploadCancelButton) uploadCancelButton.disabled = false;
      xhr.open("POST", `${streamUrl}?${query}`);
      xhr.responseType = "json";
      xhr.setRequestHeader("X-Termroom-CSRF", csrf);
      xhr.upload.addEventListener("progress", (event) => {
        const loaded = completedBytes + (event.lengthComputable ? event.loaded : 0);
        const percent = totalBytes > 0
          ? Math.min(100, Math.round((loaded / totalBytes) * 100))
          : Math.round(((index - 1) / Math.max(total, 1)) * 100);
        if (uploadProgressBar) uploadProgressBar.value = percent;
        if (uploadProgressStatus) {
          uploadProgressStatus.textContent = tr("files.upload_progress", {
            current: index,
            total,
            name: file.name,
            percent,
          });
        }
      });
      xhr.upload.addEventListener("load", () => {
        if (uploadCancelButton) uploadCancelButton.disabled = true;
        if (uploadProgressStatus) {
          uploadProgressStatus.textContent = tr("files.upload_saving", {
            current: index,
            total,
            name: file.name,
          });
        }
      });
      xhr.addEventListener("load", () => {
        activeUploadRequest = null;
        if (xhr.status >= 200 && xhr.status < 300 && xhr.response?.ok) {
          resolve();
          return;
        }
        if (xhr.status === 401) {
          reject(new Error(tr("files.upload_auth_required")));
          return;
        }
        if (xhr.status === 403) {
          reject(new Error(tr("files.upload_refresh_required")));
          return;
        }
        reject(new Error(xhr.response?.error || `HTTP ${xhr.status}`));
      });
      xhr.addEventListener("error", () => {
        activeUploadRequest = null;
        reject(new Error("Network error"));
      });
      xhr.addEventListener("abort", () => {
        activeUploadRequest = null;
        const error = new Error("Upload cancelled");
        error.name = "AbortError";
        reject(error);
      });
      xhr.send(file);
    });

  uploadForm?.addEventListener("submit", async (event) => {
    if (!uploadForm.dataset.streamUrl || !uploadInput?.files?.length) return;
    event.preventDefault();
    if (activeUploadRequest) return;
    const selectedFiles = [...uploadInput.files];
    const duplicateName = selectedFiles.find(
      (file, index) => selectedFiles.findIndex((candidate) => candidate.name === file.name) !== index,
    );
    const maxUploadBytes = Number(uploadForm.dataset.maxUploadBytes || 0);
    const oversized = maxUploadBytes > 0
      ? selectedFiles.find((file) => file.size > maxUploadBytes)
      : null;
    if (duplicateName || oversized) {
      if (uploadProgressPanel) uploadProgressPanel.hidden = false;
      if (uploadCancelButton) uploadCancelButton.disabled = true;
      if (uploadRefreshLink) uploadRefreshLink.hidden = true;
      if (uploadProgressBar) uploadProgressBar.value = 0;
      if (uploadProgressStatus) {
        uploadProgressStatus.textContent = duplicateName
          ? tr("files.error.duplicate_upload", { name: duplicateName.name })
          : tr("files.error.too_large", { size: formatBytes(maxUploadBytes) });
      }
      uploadInput.value = "";
      if (overwriteInput) overwriteInput.value = "0";
      return;
    }
    const totalBytes = selectedFiles.reduce((sum, file) => sum + file.size, 0);
    const overwrite = overwriteInput?.value === "1";
    let completedBytes = 0;
    let completedFiles = 0;
    if (uploadProgressPanel) uploadProgressPanel.hidden = false;
    if (uploadRefreshLink) uploadRefreshLink.hidden = true;
    if (uploadProgressBar) uploadProgressBar.value = 0;
    if (uploadInput) uploadInput.disabled = true;

    try {
      for (let index = 0; index < selectedFiles.length; index += 1) {
        const file = selectedFiles[index];
        await uploadOneFile({
          file,
          index: index + 1,
          total: selectedFiles.length,
          completedBytes,
          totalBytes,
          overwrite,
        });
        completedBytes += file.size;
        completedFiles += 1;
      }
      if (uploadProgressBar) uploadProgressBar.value = 100;
      const destination = uploadForm.dataset.filesUrl || location.pathname;
      const parent = uploadForm.querySelector('input[name="parent"]')?.value || ".";
      const query = new URLSearchParams({ path: parent, uploaded: String(completedFiles) });
      window.location.assign(`${destination}?${query}`);
    } catch (error) {
      if (uploadCancelButton) uploadCancelButton.disabled = true;
      if (uploadProgressStatus) {
        uploadProgressStatus.textContent =
          error?.name === "AbortError"
            ? tr("files.upload_cancelled")
            : tr("files.upload_failed", { error: error?.message || String(error) });
      }
      if (uploadRefreshLink) uploadRefreshLink.hidden = false;
      if (uploadInput) {
        uploadInput.disabled = false;
        uploadInput.value = "";
      }
      if (overwriteInput) overwriteInput.value = "0";
    }
  });

  uploadCancelButton?.addEventListener("click", () => {
    if (activeUploadRequest) activeUploadRequest.abort();
  });

  const popoverSelector =
    "details.new-terminal-menu, details.more-keys, details.terminal-manage-menu, details.create-entry, details.add-location-menu";
  const popoverDetails = [...document.querySelectorAll(popoverSelector)];
  const locationPopovers = [...document.querySelectorAll("details.add-location-menu")];

  const syncLocationModalState = () => {
    document.body.classList.toggle(
      "path-picker-modal-open",
      locationPopovers.some((details) => details.open),
    );
  };

  const closePopover = (details) => {
    if (!(details instanceof HTMLDetailsElement)) return;
    details.open = false;
    if (details.dataset.popoverClearQuery === "1" && details.dataset.popoverCloseUrl) {
      window.history.replaceState(null, "", details.dataset.popoverCloseUrl);
      details.dataset.popoverClearQuery = "0";
    }
    details.querySelector("summary")?.focus({ preventScroll: true });
  };

  locationPopovers.forEach((details) => {
    const focusFirstField = () => {
      const input = details.querySelector(".add-location-form input:not([type='hidden'])");
      window.setTimeout(() => input?.focus({ preventScroll: true }), 0);
    };
    details.addEventListener("toggle", () => {
      syncLocationModalState();
      if (details.open) focusFirstField();
    });
    if (details.open) focusFirstField();
  });
  syncLocationModalState();

  document.addEventListener("click", (event) => {
    const closeButton = event.target.closest("[data-close-popover]");
    if (closeButton) {
      closePopover(closeButton.closest("details"));
      return;
    }

    const popover = event.target.closest(popoverSelector);
    if (popover) {
      const summary = event.target.closest("summary");
      if (summary && summary.parentElement === popover && !popover.open) {
        popoverDetails.forEach((other) => {
          if (other !== popover) closePopover(other);
        });
      }
      return;
    }
    popoverDetails.forEach(closePopover);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      popoverDetails.forEach(closePopover);
      syncLocationModalState();
      return;
    }
    if (event.key !== "Tab") return;
    const modal = locationPopovers.find((details) => details.open);
    if (!modal) return;
    const focusable = [
      ...modal.querySelectorAll(
        ".add-location-form a[href], .add-location-form button:not([disabled]), .add-location-form input:not([type='hidden']):not([disabled])",
      ),
    ].filter((element) => !element.hidden && element.getClientRects().length > 0);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });

  const cleanFolderPickerQuery = () => {
    const url = new URL(window.location.href);
    [
      "browse",
      "browse_path",
      "browse_hidden",
      "browse_location",
      "location_path",
      "location_hidden",
    ].forEach((name) => url.searchParams.delete(name));
    window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
  };

  const ensureFolderPickerPanel = (form) => {
    let panel = form.querySelector("[data-folder-picker-panel]");
    if (panel) return panel;

    panel = document.createElement("section");
    panel.className = form.classList.contains("remote-workspace-form")
      ? "path-picker remote-path-picker remote-folder-browser"
      : "path-picker";
    panel.dataset.folderPickerPanel = "";
    panel.hidden = true;
    panel.setAttribute("aria-label", tr("open.browse_folders"));
    panel.innerHTML = `
      <header class="path-picker-header">
        <div><span>${tr("open.current_folder")}</span><code data-folder-picker-current></code></div>
        <div class="path-picker-header-actions">
          <a class="path-picker-control" href="#" data-folder-picker-parent hidden>↑ ${tr("open.parent_folder")}</a>
          <button class="path-picker-control path-picker-close-control" type="button" data-folder-picker-close>${tr("common.close")}</button>
        </div>
      </header>
      <button class="path-picker-control path-picker-hidden" type="button" data-folder-picker-hidden hidden></button>
      <div class="path-picker-list" data-folder-picker-list></div>
    `;

    const pathSection = form.querySelector(".remote-workspace-path-section");
    if (pathSection) {
      pathSection.insertAdjacentElement("afterend", panel);
    } else {
      const actions = form.querySelector(".path-picker-actions");
      actions?.insertAdjacentElement("afterend", panel);
    }
    return panel;
  };

  const ensureFolderPickerError = (form, panel) => {
    let error = form.querySelector(".path-picker-error");
    if (error) return error;
    error = document.createElement("p");
    error.className = "notice error path-picker-error";
    error.hidden = true;
    panel.insertAdjacentElement("beforebegin", error);
    return error;
  };

  const appendFolderPickerRow = (list, entry) => {
    const link = document.createElement("a");
    link.className = "path-picker-row";
    link.href = "#";
    link.dataset.folderPath = entry.path;
    const glyph = document.createElement("span");
    glyph.className = "file-glyph folder";
    glyph.setAttribute("aria-hidden", "true");
    const name = document.createElement("span");
    name.textContent = entry.name;
    const arrow = document.createElement("span");
    arrow.setAttribute("aria-hidden", "true");
    arrow.textContent = "›";
    link.append(glyph, name, arrow);
    list.append(link);
  };

  document.querySelectorAll("[data-folder-picker]").forEach((form) => {
    const endpoint = form.dataset.folderPickerUrl;
    const input = form.querySelector("[data-folder-picker-input]");
    const openButton = form.querySelector("[data-folder-picker-open]");
    const panel = ensureFolderPickerPanel(form);
    const errorBox = ensureFolderPickerError(form, panel);
    const submitButton = form.querySelector('button[type="submit"]');
    if (submitButton && !submitButton.dataset.folderPickerDefaultLabel) {
      submitButton.dataset.folderPickerDefaultLabel = submitButton.textContent.trim();
    }
    let request = null;
    let showHidden = panel.querySelector("[data-folder-picker-hidden]")?.dataset.showHidden === "1";

    const closePanel = () => {
      request?.abort();
      panel.hidden = true;
      panel.removeAttribute("aria-busy");
      errorBox.hidden = true;
      if (submitButton?.dataset.folderPickerDefaultLabel) {
        submitButton.textContent = submitButton.dataset.folderPickerDefaultLabel;
      }
      cleanFolderPickerQuery();
      openButton?.focus({ preventScroll: true });
    };

    const render = (data) => {
      const current = panel.querySelector("[data-folder-picker-current]");
      const parent = panel.querySelector("[data-folder-picker-parent]");
      const hiddenToggle = panel.querySelector("[data-folder-picker-hidden]");
      const list = panel.querySelector("[data-folder-picker-list]");
      if (current) current.textContent = data.current || "";
      if (input && data.current) input.value = data.current;
      if (parent) {
        parent.hidden = !data.parent;
        parent.dataset.folderPath = data.parent || "";
        parent.href = data.parent ? `#${encodeURIComponent(data.parent)}` : "#";
      }
      showHidden = Boolean(data.show_hidden);
      if (hiddenToggle) {
        const hiddenCount = Number(data.hidden_count || 0);
        hiddenToggle.hidden = hiddenCount === 0;
        hiddenToggle.dataset.hiddenCount = String(hiddenCount);
        hiddenToggle.dataset.showHidden = showHidden ? "1" : "0";
        hiddenToggle.textContent = showHidden
          ? tr("browse.hide_hidden")
          : tr("browse.show_hidden", { count: hiddenCount });
      }
      if (list) {
        list.replaceChildren();
        (data.entries || []).forEach((entry) => appendFolderPickerRow(list, entry));
        if (!data.entries?.length) {
          const empty = document.createElement("p");
          empty.className = "path-picker-empty";
          empty.textContent = tr("open.folder_picker_empty");
          list.append(empty);
        }
      }
      panel.hidden = false;
      panel.removeAttribute("aria-busy");
      errorBox.hidden = true;
      if (submitButton) submitButton.textContent = tr("open.use_current_folder");
      cleanFolderPickerQuery();
    };

    const loadFolder = async (path = "", hidden = showHidden) => {
      if (!endpoint) return;
      request?.abort();
      request = new AbortController();
      const params = new URLSearchParams();
      if (path) params.set("path", path);
      if (hidden) params.set("hidden", "1");
      panel.hidden = false;
      panel.setAttribute("aria-busy", "true");
      errorBox.hidden = true;
      try {
        const response = await fetch(`${endpoint}${params.size ? `?${params}` : ""}`, {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
          signal: request.signal,
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || `HTTP ${response.status}`);
        render(data);
      } catch (error) {
        if (error?.name === "AbortError") return;
        panel.removeAttribute("aria-busy");
        panel.hidden = true;
        errorBox.textContent = error?.message || String(error);
        errorBox.hidden = false;
      }
    };

    form.addEventListener("click", (event) => {
      const open = event.target.closest("[data-folder-picker-open]");
      if (open) {
        event.preventDefault();
        const candidate = input?.value.trim() || "";
        loadFolder(candidate.startsWith("/") ? candidate : "", false);
        return;
      }
      const close = event.target.closest("[data-folder-picker-close]");
      if (close) {
        event.preventDefault();
        closePanel();
        return;
      }
      const hiddenToggle = event.target.closest("[data-folder-picker-hidden]");
      if (hiddenToggle) {
        event.preventDefault();
        const current = panel.querySelector("[data-folder-picker-current]")?.textContent || "";
        loadFolder(current, hiddenToggle.dataset.showHidden !== "1");
        return;
      }
      const folder = event.target.closest("[data-folder-path]");
      if (folder && panel.contains(folder)) {
        event.preventDefault();
        loadFolder(folder.dataset.folderPath || "", showHidden);
      }
    });
  });

  const shareButton = document.querySelector("#file-share");
  if (shareButton && navigator.share && navigator.canShare && window.File) {
    const size = Number(shareButton.dataset.shareSize || 0);
    const name = shareButton.dataset.shareName || "download";
    const type = shareButton.dataset.shareType || "application/octet-stream";
    const maxShareBytes = 50 * 1024 * 1024;
    const probe = new File([new Uint8Array()], name, { type });
    if (size <= maxShareBytes && navigator.canShare({ files: [probe] })) {
      shareButton.hidden = false;
      shareButton.addEventListener("click", async () => {
        const previous = shareButton.textContent;
        shareButton.disabled = true;
        shareButton.textContent = tr("js.share_preparing");
        try {
          const response = await fetch(shareButton.dataset.shareUrl, {
            credentials: "same-origin",
          });
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          const blob = await response.blob();
          const file = new File([blob], name, { type: blob.type || type });
          if (!navigator.canShare({ files: [file] })) throw new Error("File sharing unavailable");
          await navigator.share({ files: [file], title: name });
        } catch (error) {
          if (error?.name !== "AbortError") {
            shareButton.textContent = tr("js.share_failed");
            window.setTimeout(() => (shareButton.textContent = previous), 1200);
          }
        } finally {
          shareButton.disabled = false;
          if (shareButton.textContent === tr("js.share_preparing")) {
            shareButton.textContent = previous;
          }
        }
      });
    }
  }

  const sshSetupForm = document.querySelector("#ssh-setup-form");
  if (sshSetupForm) {
    const probeButton = document.querySelector("#ssh-probe-button");
    const connectButton = document.querySelector("#ssh-connect-button");
    const status = document.querySelector("#ssh-setup-status");
    const fingerprintPanel = document.querySelector("#ssh-fingerprint-panel");
    const fingerprintCheckbox = document.querySelector("#ssh-fingerprint-checkbox");
    const confirmFingerprint = document.querySelector("#ssh-confirm-fingerprint");
    const hostKeyType = document.querySelector("#ssh-host-key-type");
    const hostKeyData = document.querySelector("#ssh-host-key-data");
    const hostFingerprint = document.querySelector("#ssh-host-fingerprint");

    const selectedAuthMode = () =>
      sshSetupForm.querySelector('input[name="auth_mode"]:checked')?.value || "password";

    const updateAuthPanes = () => {
      const mode = selectedAuthMode();
      sshSetupForm.querySelectorAll("[data-auth-pane]").forEach((pane) => {
        pane.hidden = pane.dataset.authPane !== mode;
      });
      const details = sshSetupForm.querySelector(".ssh-existing-key-details");
      if (details instanceof HTMLDetailsElement) details.open = mode === "existing";
    };

    const resetProbe = () => {
      if (fingerprintPanel) fingerprintPanel.hidden = true;
      if (connectButton) {
        connectButton.hidden = true;
        connectButton.disabled = true;
      }
      if (fingerprintCheckbox) fingerprintCheckbox.checked = false;
      if (confirmFingerprint) confirmFingerprint.value = "0";
      if (hostKeyType) hostKeyType.value = "";
      if (hostKeyData) hostKeyData.value = "";
      if (hostFingerprint) hostFingerprint.value = "";
      if (status) {
        status.textContent = "";
        status.classList.remove("error", "success");
      }
    };

    sshSetupForm.querySelectorAll('input[name="auth_mode"]').forEach((input) => {
      input.addEventListener("change", updateAuthPanes);
    });
    sshSetupForm
      .querySelectorAll('input[name="target"], input[name="username"], input[name="port"]')
      .forEach((input) => input.addEventListener("input", resetProbe));

    fingerprintCheckbox?.addEventListener("change", () => {
      const confirmed = fingerprintCheckbox.checked;
      if (confirmFingerprint) confirmFingerprint.value = confirmed ? "1" : "0";
      if (connectButton) connectButton.disabled = !confirmed;
    });

    const probe = async () => {
      if (!sshSetupForm.reportValidity()) return;
      const data = new FormData(sshSetupForm);
      data.delete("password");
      data.delete("host_key_type");
      data.delete("host_key_data");
      data.delete("host_fingerprint");
      data.delete("confirm_fingerprint");
      probeButton.disabled = true;
      connectButton.hidden = true;
      status.textContent = tr("ssh.add.probing");
      status.classList.remove("error", "success");
      try {
        const response = await fetch(sshSetupForm.dataset.probeUrl, {
          method: "POST",
          body: data,
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        const result = await response.json();
        if (!response.ok || !result.ok) {
          throw new Error(result.error || tr("ssh.add.probe_failed"));
        }
        hostKeyType.value = result.host_key_type;
        hostKeyData.value = result.host_key_data;
        hostFingerprint.value = result.host_fingerprint;
        document.querySelector("#ssh-probed-address").textContent =
          `${result.username}@${result.host}:${result.port}`;
        document.querySelector("#ssh-probed-fingerprint").textContent = result.host_fingerprint;
        document.querySelector("#ssh-probed-key-type").textContent = result.host_key_type;
        fingerprintCheckbox.checked = false;
        confirmFingerprint.value = "0";
        fingerprintPanel.hidden = false;
        connectButton.hidden = false;
        connectButton.disabled = true;
        status.textContent = tr("ssh.add.probe_success");
        status.classList.add("success");
        fingerprintPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
      } catch (error) {
        resetProbe();
        status.textContent = error instanceof Error ? error.message : String(error);
        status.classList.add("error");
      } finally {
        probeButton.disabled = false;
      }
    };

    probeButton?.addEventListener("click", probe);
    sshSetupForm.addEventListener("submit", (event) => {
      if (confirmFingerprint?.value !== "1") {
        event.preventDefault();
        probe();
        return;
      }
      const mode = selectedAuthMode();
      const password = sshSetupForm.querySelector('input[name="password"]');
      if (mode === "password" && !password?.value) {
        event.preventDefault();
        password?.focus();
        status.textContent = tr("ssh.add.password_required");
        status.classList.add("error");
      }
    });
    const optionalName = sshSetupForm.querySelector(".ssh-optional-name");
    const nameInput = sshSetupForm.querySelector('input[name="name"]');
    if (optionalName instanceof HTMLDetailsElement) optionalName.open = Boolean(nameInput?.value);
    updateAuthPanes();
  }

  const scrollbackSearch = document.querySelector("#scrollback-search");
  const scrollbackOutput = document.querySelector("#scrollback-output");
  const scrollbackSearchStatus = document.querySelector("#scrollback-search-status");
  let scrollbackNextOffset = 0;
  const selectTextOffsets = (root, start, end) => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let position = 0;
    let startNode = null;
    let startOffset = 0;
    let endNode = null;
    let endOffset = 0;
    while (walker.nextNode()) {
      const node = walker.currentNode;
      const next = position + node.data.length;
      if (!startNode && start >= position && start <= next) {
        startNode = node;
        startOffset = Math.min(node.data.length, start - position);
      }
      if (end >= position && end <= next) {
        endNode = node;
        endOffset = Math.min(node.data.length, end - position);
        break;
      }
      position = next;
    }
    if (!startNode || !endNode) return false;
    const range = document.createRange();
    range.setStart(startNode, startOffset);
    range.setEnd(endNode, endOffset);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
    const rect = range.getBoundingClientRect();
    if (rect.height || rect.width) {
      window.scrollTo({
        top: Math.max(0, window.scrollY + rect.top - window.innerHeight * 0.35),
        behavior: "smooth",
      });
    }
    return true;
  };
  const findNextScrollbackMatch = () => {
    if (!scrollbackSearch || !scrollbackOutput) return;
    const query = scrollbackSearch.value;
    if (!query) {
      scrollbackNextOffset = 0;
      if (scrollbackSearchStatus) scrollbackSearchStatus.textContent = "";
      return;
    }
    const content = scrollbackOutput.textContent || "";
    const folded = content.toLocaleLowerCase();
    const needle = query.toLocaleLowerCase();
    let index = folded.indexOf(needle, scrollbackNextOffset);
    if (index < 0 && scrollbackNextOffset > 0) index = folded.indexOf(needle);
    if (index < 0) {
      if (scrollbackSearchStatus) {
        scrollbackSearchStatus.textContent = tr("scrollback.no_match");
      }
      return;
    }
    scrollbackNextOffset = index + Math.max(1, query.length);
    selectTextOffsets(scrollbackOutput, index, index + query.length);
    if (scrollbackSearchStatus) scrollbackSearchStatus.textContent = "";
  };
  scrollbackSearch?.addEventListener("input", () => {
    scrollbackNextOffset = 0;
    if (scrollbackSearchStatus) scrollbackSearchStatus.textContent = "";
  });
  scrollbackSearch?.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    findNextScrollbackMatch();
  });

  const editor = document.querySelector("#editor-content");
  const editorForm = document.querySelector("#editor-form");
  if (!editor || !editorForm) return;

  const initialContent = editor.value;
  const initiallyUnsaved = editor.dataset.unsaved === "1";
  let submitted = false;
  editorForm.addEventListener("submit", () => {
    submitted = true;
  });
  window.addEventListener("beforeunload", (event) => {
    if (!submitted && (initiallyUnsaved || editor.value !== initialContent)) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  const replaceSelection = (replacement, selectionStart = null, selectionEnd = null) => {
    const start = editor.selectionStart;
    const end = editor.selectionEnd;
    editor.setRangeText(replacement, start, end, "select");
    if (selectionStart !== null) editor.selectionStart = selectionStart;
    if (selectionEnd !== null) editor.selectionEnd = selectionEnd;
    editor.dispatchEvent(new InputEvent("input", { bubbles: true }));
    editor.focus();
  };

  document.querySelectorAll("[data-editor-action]").forEach((button) => {
    button.addEventListener("click", () => {
      const action = button.dataset.editorAction;
      if (action === "undo" || action === "redo") {
        document.execCommand(action);
        editor.focus();
        return;
      }
      if (action === "find") {
        const bar = document.querySelector("#find-bar");
        bar.hidden = !bar.hidden;
        if (!bar.hidden) document.querySelector("#find-input")?.focus();
        return;
      }
      if (action === "indent" || action === "outdent") {
        const value = editor.value;
        const start = editor.selectionStart;
        const end = editor.selectionEnd;
        const lineStart = value.lastIndexOf("\n", start - 1) + 1;
        const lineEndIndex = value.indexOf("\n", end);
        const lineEnd = lineEndIndex === -1 ? value.length : lineEndIndex;
        const selectedLines = value.slice(lineStart, lineEnd);
        const changed = selectedLines
          .split("\n")
          .map((line) =>
            action === "indent" ? `    ${line}` : line.replace(/^( {1,4}|\t)/, ""),
          )
          .join("\n");
        editor.setSelectionRange(lineStart, lineEnd);
        replaceSelection(changed, lineStart, lineStart + changed.length);
      }
    });
  });

  const findInput = document.querySelector("#find-input");
  const findNext = () => {
    const query = findInput?.value;
    if (!query) return;
    const content = editor.value.toLocaleLowerCase();
    const needle = query.toLocaleLowerCase();
    let index = content.indexOf(needle, editor.selectionEnd);
    if (index < 0) index = content.indexOf(needle);
    if (index >= 0) {
      editor.focus();
      editor.setSelectionRange(index, index + query.length);
    }
  };
  document.querySelector("#find-next")?.addEventListener("click", findNext);
  findInput?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      findNext();
    }
  });

  editor.addEventListener("keydown", (event) => {
    if (event.key === "Tab") {
      event.preventDefault();
      replaceSelection("    ");
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      editorForm.requestSubmit();
    }
  });

  document.querySelector("#show-diff")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const dialog = document.querySelector("#diff-dialog");
    const output = document.querySelector("#diff-output");
    output.textContent = tr("js.diff_loading");
    dialog.showModal();
    try {
      const response = await fetch(button.dataset.diffUrl, {
        method: "POST",
        body: new FormData(editorForm),
        credentials: "same-origin",
      });
      const text = await response.text();
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      output.textContent = text;
    } catch (error) {
      output.textContent = tr("js.diff_failed", { error });
    }
  });
})();
