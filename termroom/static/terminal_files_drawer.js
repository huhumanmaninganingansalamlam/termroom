(() => {
  "use strict";
  // Terminal Files is read-only: fetch existing authenticated HTML partials,
  // do not duplicate Files mutations or remount the xterm/PTY.
  // A's Files drawer reads the existing authenticated file-results response.
  // It does not clone file-mutation controls or remount the xterm/PTY.
  const terminalFilesDrawer = document.querySelector("[data-terminal-files-drawer]");
  const terminalFilesTrigger = document.querySelector("[data-terminal-files-trigger]");
  if (
    terminalFilesDrawer &&
    typeof terminalFilesDrawer.showModal === "function" &&
    terminalFilesTrigger
  ) {
    const workspaceId = terminalFilesDrawer.dataset.workspaceId;
    const workspaceBase = "/w/" + encodeURIComponent(workspaceId);
    const list = terminalFilesDrawer.querySelector("[data-terminal-files-list]");
    const state = terminalFilesDrawer.querySelector("[data-terminal-files-state]");
    const locationLabel = terminalFilesDrawer.querySelector("[data-terminal-files-location]");
    const pagination = terminalFilesDrawer.querySelector("[data-terminal-files-pagination]");
    const queryInput = terminalFilesDrawer.querySelector("#terminal-files-drawer-query");
    const searchForm = terminalFilesDrawer.querySelector("[data-terminal-files-search]");
    const closeButton = terminalFilesDrawer.querySelector("[data-terminal-files-close]");
    const retryButton = terminalFilesDrawer.querySelector("[data-terminal-files-retry]");
    let directory = ".";
    let currentPage = 1;
    let serial = 0;
    let controller = null;

    function fileLink(raw, mode) {
      try {
        const target = new URL(raw, window.location.href);
        if (target.origin !== window.location.origin) return null;
        if (mode === "folder" && target.pathname === workspaceBase + "/files") return target;
        if (mode === "file" && target.pathname.startsWith(workspaceBase + "/view/")) return target;
      } catch {
        // Ignore malformed links in an unexpected partial response.
      }
      return null;
    }

    function renderFileResults(markup) {
      const doc = new DOMParser().parseFromString(markup, "text/html");
      if (!doc.querySelector("[data-file-search-metadata]") || !doc.querySelector(".workspace-file-list")) {
        throw new Error("Unexpected file-list response");
      }
      const fragment = document.createDocumentFragment();
      const rows = doc.querySelectorAll(".file-browser-row");
      for (const row of rows) {
        const source = row.matches("a.parent-row") ? row : row.querySelector("a.file-primary");
        if (!source) continue;
        const folder = row.classList.contains("parent-row")
          || Boolean(source.querySelector(".file-glyph.folder"));
        const target = fileLink(source.getAttribute("href"), folder ? "folder" : "file");
        if (!target) continue;
        const nameNode = source.querySelector(".file-name");
        const filename = nameNode?.querySelector("span")?.textContent.trim()
          || nameNode?.textContent.trim() || source.textContent.trim();
        const parent = nameNode?.querySelector("small")?.textContent.trim();
        const name = parent ? filename + " — " + parent : filename;
        const item = document.createElement("li");
        const link = document.createElement("a");
        const glyph = document.createElement("span");
        glyph.className = "terminal-files-drawer-glyph";
        glyph.setAttribute("aria-hidden", "true");
        glyph.textContent = folder ? "▸" : "·";
        link.href = target.href;
        link.append(glyph, document.createTextNode(name));
        if (folder) {
          link.addEventListener("click", (event) => {
            event.preventDefault();
            queryInput.value = "";
            void loadFiles(target.searchParams.get("path") || ".", "", 1);
          });
        } else {
          link.target = "_blank";
          link.rel = "noopener";
          link.setAttribute(
            "aria-label",
            name + " (" + terminalFilesDrawer.dataset.newTabLabel + ")",
          );
          const hint = document.createElement("small");
          hint.textContent = terminalFilesDrawer.dataset.newTabLabel;
          link.appendChild(hint);
        }
        item.appendChild(link);
        fragment.appendChild(item);
      }
      if (!fragment.childNodes.length) {
        const empty = document.createElement("li");
        empty.className = "terminal-files-drawer-empty";
        empty.textContent = doc.querySelector(".empty-state")?.textContent.trim() || "";
        fragment.appendChild(empty);
      }
      list.replaceChildren(fragment);
      const pager = doc.querySelector(".file-pagination");
      const controls = document.createDocumentFragment();
      if (pager) {
        for (const source of pager.querySelectorAll("a, .file-pagination-status")) {
          if (source.tagName !== "A") {
            const label = document.createElement("span");
            label.textContent = source.textContent.trim();
            controls.appendChild(label);
            continue;
          }
          const target = fileLink(source.getAttribute("href"), "folder");
          if (!target) continue;
          const link = document.createElement("a");
          link.href = target.href;
          link.textContent = source.textContent.trim();
          link.addEventListener("click", (event) => {
            event.preventDefault();
            void loadFiles(
              target.searchParams.get("path") || ".",
              target.searchParams.get("q") || "",
              Number(target.searchParams.get("page")) || 1,
            );
          });
          controls.appendChild(link);
        }
      }
      pagination.replaceChildren(controls);
      pagination.setAttribute("aria-label", terminalFilesDrawer.dataset.paginationLabel);
      locationLabel.textContent = directory === "."
        ? terminalFilesDrawer.dataset.rootLabel : directory;
      return {
        count: doc.querySelectorAll(".file-row-with-actions").length,
        warning: doc.querySelector(".file-search-summary strong")?.textContent.trim() || "",
      };
    }

    async function loadFiles(path, query, page) {
      controller?.abort();
      controller = new AbortController();
      const requestId = ++serial;
      directory = path;
      currentPage = page;
      const url = new URL(workspaceBase + "/files", window.location.origin);
      url.searchParams.set("path", path);
      if (query) url.searchParams.set("q", query);
      if (page > 1) url.searchParams.set("page", String(page));
      list.setAttribute("aria-busy", "true");
      state.textContent = terminalFilesDrawer.dataset.loadingLabel;
      retryButton.hidden = true;
      locationLabel.textContent = path === "."
        ? terminalFilesDrawer.dataset.rootLabel : path;
      try {
        const response = await fetch(url.href, {
          credentials: "same-origin",
          cache: "no-store",
          headers: { "x-termroom-partial": "file-results" },
          signal: controller.signal,
        });
        if (!response.ok || !response.headers.get("content-type")?.includes("text/html")) {
          throw new Error("File list request was not successful");
        }
        const markup = await response.text();
        if (requestId !== serial || !terminalFilesDrawer.open) return;
        const result = renderFileResults(markup);
        state.textContent = terminalFilesDrawer.dataset.loadedLabel.replace("{count}", String(result.count))
          + (result.warning ? " · " + result.warning : "");
      } catch (error) {
        if (error?.name === "AbortError" || requestId !== serial) return;
        list.replaceChildren();
        pagination.replaceChildren();
        state.textContent = terminalFilesDrawer.dataset.errorLabel;
        retryButton.hidden = false;
      } finally {
        if (requestId === serial) list.setAttribute("aria-busy", "false");
      }
    }

    terminalFilesTrigger.addEventListener("click", (event) => {
      event.preventDefault();
      if (terminalFilesDrawer.open) return;
      terminalFilesDrawer.showModal();
      closeButton.focus({ preventScroll: true });
      void loadFiles(directory, queryInput.value.trim().slice(0, 120), currentPage);
    });
    closeButton.addEventListener("click", () => terminalFilesDrawer.close());
    retryButton.addEventListener("click", () => {
      void loadFiles(directory, queryInput.value.trim().slice(0, 120), currentPage);
    });
    searchForm.addEventListener("submit", (event) => {
      event.preventDefault();
      void loadFiles(directory, queryInput.value.trim().slice(0, 120), 1);
    });
    terminalFilesDrawer.addEventListener("click", (event) => {
      if (event.target !== terminalFilesDrawer) return;
      const rect = terminalFilesDrawer.getBoundingClientRect();
      if (
        event.clientX < rect.left || event.clientX > rect.right ||
        event.clientY < rect.top || event.clientY > rect.bottom
      ) terminalFilesDrawer.close();
    });
    terminalFilesDrawer.addEventListener("close", () => {
      serial += 1;
      controller?.abort();
      controller = null;
      list.setAttribute("aria-busy", "false");
      if (terminalFilesTrigger.getClientRects().length) {
        terminalFilesTrigger.focus({ preventScroll: true });
      }
    });
  }

})();
