(() => {
  "use strict";

  // Terminal copy controls own only UI/Clipboard API; native xterm and
  // reading-mode transitions remain owned by terminal_selection.js and
  // mobile_scrollback.js. No second copy-mode state is introduced.
  const installTerminalCopyControls = ({
    terminalHost,
    selectionOwnership,
    getSelectionMode,
    applySelectionOwnership,
    mouseTrackingActive,
    atLiveBottom,
  }) => {
    const tr = (key) => window.TermroomT?.(key) || key;
    const copyModeButton = document.querySelector("#terminal-copy-mode");
    const copyControls = document.querySelector("#terminal-copy-controls");
    const copySelectionButton = document.querySelector("#terminal-copy-selection");
    const copyVisibleButton = document.querySelector("#terminal-copy-visible");
    const copyStatus = document.querySelector("#terminal-copy-status");

    const syncTerminalCopyActions = () => {
      if (!copyModeButton || !copyControls) return;
      const selecting = getSelectionMode() === selectionOwnership.COPY_XTERM;
      copyModeButton.disabled = typeof terminalHost.termroomSetCopyMode !== "function";
      copyModeButton.setAttribute("aria-pressed", String(selecting));
      copyModeButton.setAttribute("aria-label", tr(
        selecting ? "terminal.copy_mode_off" : "terminal.copy_mode_on",
      ));
      copyModeButton.title = copyModeButton.getAttribute("aria-label");
      copyModeButton.querySelector(".terminal-tool-label").textContent =
        copyModeButton.getAttribute("aria-label");
      copyModeButton.querySelector(".terminal-copy-mobile-label").textContent =
        tr(selecting ? "terminal.copy_mode_short_off" : "terminal.copy_mode_short");
      copyControls.hidden = !selecting;
      if (copySelectionButton) {
        copySelectionButton.disabled = !selecting || !terminalHost.termroomGetSelection?.();
      }
      if (copyVisibleButton) {
        copyVisibleButton.disabled = !selecting || !terminalHost.termroomGetVisibleText?.();
      }
    };

    const writeTerminalClipboard = async (text) => {
      if (!copyStatus) return;
      if (!text) {
        copyStatus.textContent = tr("terminal.copy_empty");
        return;
      }
      if (!window.isSecureContext || typeof navigator.clipboard?.writeText !== "function") {
        copyStatus.textContent = tr("terminal.copy_error");
        return;
      }
      try {
        await navigator.clipboard.writeText(text);
        if (getSelectionMode() === selectionOwnership.COPY_XTERM) {
          copyStatus.textContent = tr("terminal.copy_success");
        }
      } catch {
        copyStatus.textContent = tr("terminal.copy_error");
      }
    };
    const copyTerminalSelection = () =>
      writeTerminalClipboard(terminalHost.termroomGetSelection?.() || "");
    const copyVisibleTerminal = () =>
      writeTerminalClipboard(terminalHost.termroomGetVisibleText?.() || "");

    copyModeButton?.addEventListener("click", () => {
      if (typeof terminalHost.termroomSetCopyMode !== "function") return;
      applySelectionOwnership({
        type: getSelectionMode() === selectionOwnership.COPY_XTERM ? "stop-copy" : "start-copy",
        mouseTracking: mouseTrackingActive(),
        away: !atLiveBottom(),
      });
    });
    copySelectionButton?.addEventListener("click", () => { void copyTerminalSelection(); });
    copyVisibleButton?.addEventListener("click", () => { void copyVisibleTerminal(); });
    terminalHost.addEventListener("termroom:terminal-selection-changed", syncTerminalCopyActions);
    window.addEventListener("termroom:terminal-copy-ready", syncTerminalCopyActions);
    syncTerminalCopyActions();

    const showError = () => {
      if (copyStatus) copyStatus.textContent = tr("terminal.copy_error");
    };
    const clearStatus = () => {
      if (copyStatus) copyStatus.textContent = "";
    };
    return Object.freeze({
      sync: syncTerminalCopyActions,
      showError,
      clearStatus,
    });
  };

  window.TermroomTerminalCopy = Object.freeze({ installTerminalCopyControls });
})();
