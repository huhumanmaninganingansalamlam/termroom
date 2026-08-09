(() => {
  const host = document.querySelector("#terminal");
  if (!host || typeof window.Terminal !== "function") return;
  const messages = window.TermroomI18n || {};
  const fallbackTranslate = (key, values = {}) => {
    let text = messages[key] || key;
    Object.entries(values).forEach(([name, value]) => {
      text = text.replaceAll(`{${name}}`, String(value));
    });
    return text;
  };
  const tr = window.TermroomT || fallbackTranslate;

  const TERMINAL_FONT_SIZE_KEY = "termroom.terminal.font-size";
  const DEFAULT_TERMINAL_FONT_SIZE = 14;
  const MIN_TERMINAL_FONT_SIZE = 12;
  const MAX_TERMINAL_FONT_SIZE = 20;
  const clampTerminalFontSize = (value) =>
    Math.max(
      MIN_TERMINAL_FONT_SIZE,
      Math.min(MAX_TERMINAL_FONT_SIZE, Number(value) || DEFAULT_TERMINAL_FONT_SIZE),
    );
  const loadTerminalFontSize = () => {
    try {
      return clampTerminalFontSize(window.localStorage.getItem(TERMINAL_FONT_SIZE_KEY));
    } catch {
      return DEFAULT_TERMINAL_FONT_SIZE;
    }
  };
  const initialTerminalFontSize = loadTerminalFontSize();

  const terminalTheme = () => {
    const styles = window.getComputedStyle(document.documentElement);
    const color = (name, fallback) => styles.getPropertyValue(name).trim() || fallback;
    const background = color("--terminal-bg", "#151b23");
    return {
      background,
      foreground: color("--terminal-text", "#d1d7e0"),
      cursor: color("--terminal-cursor", "#adbbff"),
      cursorAccent: background,
      selectionBackground: color("--terminal-selection", "#3b4965"),
      black: color("--terminal-black", "#20262e"),
      brightBlack: color("--terminal-bright-black", "#7f8996"),
      red: color("--terminal-red", "#ff8585"),
      brightRed: color("--terminal-bright-red", "#ffaaaa"),
      green: color("--terminal-green", "#57d09b"),
      brightGreen: color("--terminal-bright-green", "#7de2b4"),
      yellow: color("--terminal-yellow", "#e7bd68"),
      brightYellow: color("--terminal-bright-yellow", "#f3d38d"),
      blue: color("--terminal-blue", "#8fa4ff"),
      brightBlue: color("--terminal-bright-blue", "#adbbff"),
      magenta: color("--terminal-magenta", "#cba2e8"),
      brightMagenta: color("--terminal-bright-magenta", "#dfbdf3"),
      cyan: color("--terminal-cyan", "#71c8d4"),
      brightCyan: color("--terminal-bright-cyan", "#91dce4"),
      white: color("--terminal-white", "#d1d7e0"),
      brightWhite: color("--terminal-bright-white", "#f3f6fa"),
    };
  };

  const terminalFontFamily = () => {
    const configured = window
      .getComputedStyle(document.documentElement)
      .getPropertyValue("--font-mono")
      .trim();
    return (
      configured ||
      'ui-monospace, "SFMono-Regular", Menlo, Monaco, Consolas, "Cascadia Mono", "Segoe UI Mono", "Noto Sans Mono CJK KR", D2Coding, "Nanum Gothic Coding", "Liberation Mono", monospace'
    );
  };

  const status = document.querySelector("#terminal-status");
  const statusLabel = status.querySelector(".terminal-status-label");
  const term = new window.Terminal({
    cursorBlink: true,
    cursorStyle: "bar",
    fontFamily: terminalFontFamily(),
    fontSize: initialTerminalFontSize,
    lineHeight: 1.2,
    scrollback: 5000,
    allowProposedApi: false,
    theme: terminalTheme(),
  });
  term.open(host);
  window.addEventListener("themechange", () => {
    term.options.theme = terminalTheme();
    term.options.fontFamily = terminalFontFamily();
    term.refresh(0, Math.max(0, term.rows - 1));
  });

  // xterm owns the actual IME/composition lifecycle. Do not mirror the hidden
  // textarea ourselves: composition updates must only reach the PTY after
  // xterm has committed them. We only disable browser text transforms that
  // make shell input surprising on phones.
  const terminalTextarea = term.textarea || host.querySelector(".xterm-helper-textarea");
  if (terminalTextarea) {
    terminalTextarea.autocomplete = "off";
    terminalTextarea.autocapitalize = "off";
    terminalTextarea.setAttribute("autocorrect", "off");
    terminalTextarea.spellcheck = false;
    terminalTextarea.inputMode = "text";
    terminalTextarea.setAttribute("enterkeyhint", "enter");
    terminalTextarea.setAttribute("aria-label", tr("terminal.input_direct_aria"));
    terminalTextarea.addEventListener("compositionstart", () =>
      host.classList.add("ime-composing"),
    );
    terminalTextarea.addEventListener("compositionend", () =>
      host.classList.remove("ime-composing"),
    );
  }

  let socket = null;
  let reconnectTimer = null;
  let reconnectDelay = 500;
  let isConnected = false;
  let lastInputRevision = 0;
  let presenceInitialized = false;
  let otherInputTimer = null;
  const mobileInput = window.matchMedia("(max-width: 1023px)");
  const coarsePrimaryPointer = window.matchMedia("(pointer: coarse)");
  const composePane = document.querySelector(".terminal-compose-pane");
  const composerRoot = document.querySelector(".terminal-composer");
  const openCommandEditor = document.querySelector("#open-command-editor");
  const closeCommandEditor = document.querySelector("#close-command-editor");
  const moreKeys = document.querySelector("details.more-keys");
  let composerOpen = false;
  let mobileViewportBaseline = window.visualViewport?.height || window.innerHeight;
  let mobileViewportBaselineWidth = window.visualViewport?.width || window.innerWidth;

  const setStatusMessage = (message) => {
    statusLabel.textContent = message;
    status.setAttribute("aria-label", message);
  };

  const setStatus = (message, connected = false) => {
    isConnected = connected;
    setStatusMessage(message);
    status.classList.toggle("connected", connected);
  };

  const updatePresence = async () => {
    if (!isConnected) return;
    try {
      const response = await fetch(`/api/terminals/${host.dataset.terminalId}/presence`, {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!response.ok) return;
      const presence = await response.json();
      const count = Number(presence.count || 0);
      const revision = Number(presence.input_revision || 0);
      if (!presenceInitialized) {
        presenceInitialized = true;
        lastInputRevision = revision;
        setStatusMessage(
          count > 1
            ? tr("terminal.status.connected_many", { count })
            : tr("terminal.status.connected")
        );
        return;
      }
      const otherDevice =
        revision > lastInputRevision &&
        presence.last_input_device_id &&
        presence.last_input_device_id !== host.dataset.deviceId;
      lastInputRevision = Math.max(lastInputRevision, revision);
      window.clearTimeout(otherInputTimer);
      if (otherDevice) {
        setStatusMessage(
          count > 1
            ? tr("terminal.status.other_input_many", { count })
            : tr("terminal.status.other_input")
        );
        otherInputTimer = window.setTimeout(updatePresence, 1800);
        return;
      }
      setStatusMessage(
        count > 1
          ? tr("terminal.status.connected_many", { count })
          : tr("terminal.status.connected")
      );
    } catch {
      // The WebSocket status remains the source of truth when polling fails.
    }
  };

  const send = (payload) => {
    if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(payload));
  };

  const resizeTerminal = (forceSend = false) => {
    const cell = term._core?._renderService?.dimensions?.css?.cell;
    if (!cell?.width || !cell?.height) return;

    const style = window.getComputedStyle(host);
    const horizontalPadding = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
    const verticalPadding = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);
    const scrollbarWidth = term._core?.viewport?.scrollBarWidth || 0;
    const width = Math.max(host.clientWidth - horizontalPadding - scrollbarWidth, cell.width * 20);
    const height = Math.max(host.clientHeight - verticalPadding, cell.height * 4);
    const cols = Math.max(20, Math.floor(width / cell.width));
    const rows = Math.max(4, Math.floor(height / cell.height));
    const changed = term.cols !== cols || term.rows !== rows;
    if (changed) {
      term.resize(cols, rows);
    }
    if (changed || forceSend) send({ kind: "resize", rows, cols });
  };

  const scheduleResize = (forceSend = false) =>
    window.requestAnimationFrame(() => resizeTerminal(forceSend));

  const fontSizeValue = document.querySelector("#terminal-font-size-value");
  const fontSizeDecrease = document.querySelector("#terminal-font-decrease");
  const fontSizeIncrease = document.querySelector("#terminal-font-increase");
  const fontSizeReset = document.querySelector("#terminal-font-reset");
  const updateFontSizeControls = (size) => {
    if (fontSizeValue) {
      fontSizeValue.textContent = tr("terminal.font_size_value", { size });
    }
    if (fontSizeDecrease) fontSizeDecrease.disabled = size <= MIN_TERMINAL_FONT_SIZE;
    if (fontSizeIncrease) fontSizeIncrease.disabled = size >= MAX_TERMINAL_FONT_SIZE;
    if (fontSizeReset) fontSizeReset.disabled = size === DEFAULT_TERMINAL_FONT_SIZE;
  };
  const applyTerminalFontSize = (value, { persist = true } = {}) => {
    const size = clampTerminalFontSize(value);
    term.options.fontSize = size;
    updateFontSizeControls(size);
    if (persist) {
      try {
        window.localStorage.setItem(TERMINAL_FONT_SIZE_KEY, String(size));
      } catch {
        // Storage is optional; the live terminal still updates immediately.
      }
    }
    term.refresh(0, Math.max(0, term.rows - 1));
    window.setTimeout(() => scheduleResize(true), 0);
  };
  updateFontSizeControls(initialTerminalFontSize);
  fontSizeDecrease?.addEventListener("click", () =>
    applyTerminalFontSize(term.options.fontSize - 1),
  );
  fontSizeIncrease?.addEventListener("click", () =>
    applyTerminalFontSize(term.options.fontSize + 1),
  );
  fontSizeReset?.addEventListener("click", () =>
    applyTerminalFontSize(DEFAULT_TERMINAL_FONT_SIZE),
  );
  window.addEventListener("storage", (event) => {
    if (event.key === TERMINAL_FONT_SIZE_KEY && event.newValue) {
      applyTerminalFontSize(event.newValue, { persist: false });
    }
  });

  const setComposerOpen = (open, { focus = true } = {}) => {
    composerOpen = Boolean(open);
    composerRoot?.classList.toggle("command-editor-open", composerOpen);
    if (composePane) composePane.hidden = !composerOpen;
    if (focus) {
      if (composerOpen) {
        document.querySelector("#command-input")?.focus();
      } else {
        term.focus();
      }
    }
    window.setTimeout(() => scheduleResize(true), 0);
  };

  const updateMobileKeyboardState = () => {
    const viewport = window.visualViewport;
    const height = viewport?.height || window.innerHeight;
    const width = viewport?.width || window.innerWidth;
    const widthChanged =
      Math.abs(width - mobileViewportBaselineWidth) >
      Math.max(80, mobileViewportBaselineWidth * 0.2);
    if (widthChanged) {
      mobileViewportBaseline = height;
      mobileViewportBaselineWidth = width;
    } else if (height > mobileViewportBaseline) {
      mobileViewportBaseline = height;
    }
    const active = document.activeElement;
    const textInputFocused = active === terminalTextarea || active === commandInput;
    const layoutGap = viewport ? Math.max(0, window.innerHeight - viewport.height) : 0;
    const keyboardThreshold = Math.max(100, mobileViewportBaseline * 0.18);
    const keyboardOpen =
      mobileInput.matches &&
      textInputFocused &&
      !widthChanged &&
      (layoutGap > keyboardThreshold || mobileViewportBaseline - height > keyboardThreshold);
    document.body.classList.toggle("terminal-keyboard-open", keyboardOpen);
    window.setTimeout(() => scheduleResize(true), 0);
  };

  openCommandEditor?.addEventListener("click", () => setComposerOpen(true));
  closeCommandEditor?.addEventListener("click", () => setComposerOpen(false));
  terminalTextarea?.addEventListener("focus", () => {
    if (mobileInput.matches && composerOpen) {
      setComposerOpen(false, { focus: false });
    }
  });
  setComposerOpen(false, { focus: false });

  const claimVisibleTerminal = () => {
    if (document.visibilityState !== "visible") return;
    send({ kind: "claim" });
    scheduleResize(true);
  };

  const connect = () => {
    window.clearTimeout(reconnectTimer);
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${scheme}://${location.host}/ws/terminal/${host.dataset.terminalId}`);
    setStatus(tr("terminal.status.connecting"));

    socket.addEventListener("open", () => {
      reconnectDelay = 500;
      setStatus(tr("terminal.status.connected"), true);
      claimVisibleTerminal();
      updatePresence();
      if (!coarsePrimaryPointer.matches) term.focus();
    });
    socket.addEventListener("message", (event) => term.write(event.data));
    socket.addEventListener("close", (event) => {
      const terminalCloseMessages = {
        4401: tr("terminal.status.auth_required"),
        4403: tr("terminal.status.rejected"),
        4404: tr("terminal.status.not_found"),
        4429: tr("terminal.status.too_many"),
      };
      const terminalCloseMessage = terminalCloseMessages[event.code];
      setStatus(terminalCloseMessage || tr("terminal.status.reconnecting"));
      if (!terminalCloseMessage && document.visibilityState !== "hidden") {
        reconnectTimer = window.setTimeout(connect, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 1.7, 5000);
      }
    });
    socket.addEventListener("error", () => socket.close());
  };

  term.onData((data) => send({ kind: "input", data }));
  new ResizeObserver(() => scheduleResize()).observe(host);
  window.visualViewport?.addEventListener("resize", () => {
    updateMobileKeyboardState();
    window.setTimeout(() => scheduleResize(), 50);
  });
  terminalTextarea?.addEventListener("focus", updateMobileKeyboardState);
  terminalTextarea?.addEventListener("blur", () => window.setTimeout(updateMobileKeyboardState, 0));
  document.fonts?.ready.then(() => scheduleResize());
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    if (socket?.readyState === WebSocket.CLOSED) {
      connect();
      return;
    }
    claimVisibleTerminal();
  });
  window.addEventListener("focus", claimVisibleTerminal);
  window.addEventListener("orientationchange", () => {
    window.setTimeout(() => {
      mobileViewportBaseline = window.visualViewport?.height || window.innerHeight;
      mobileViewportBaselineWidth = window.visualViewport?.width || window.innerWidth;
      updateMobileKeyboardState();
    }, 250);
  });
  window.setInterval(updatePresence, 2500);

  const decodeKey = (value) =>
    value
      .replace(/\\u([0-9a-fA-F]{4})/g, (_, code) => String.fromCharCode(parseInt(code, 16)))
      .replace(/\\t/g, "\t");

  const terminalActionValue = (action) => {
    const application = term.modes.applicationCursorKeysMode;
    switch (action) {
      case "arrow-up":
        return application ? "\u001bOA" : "\u001b[A";
      case "arrow-down":
        return application ? "\u001bOB" : "\u001b[B";
      case "arrow-right":
        return application ? "\u001bOC" : "\u001b[C";
      case "arrow-left":
        return application ? "\u001bOD" : "\u001b[D";
      default:
        return "";
    }
  };

  let ctrlArmed = false;
  const ctrlButton = document.querySelector("#ctrl-key");
  const closeMoreKeys = ({ focus = true } = {}) => {
    if (moreKeys) moreKeys.open = false;
    if (focus) term.focus();
  };
  ctrlButton?.addEventListener("click", () => {
    ctrlArmed = !ctrlArmed;
    ctrlButton.setAttribute("aria-pressed", String(ctrlArmed));
    term.focus();
  });

  document.querySelectorAll("[data-terminal-key], [data-terminal-action]").forEach((button) => {
    button.addEventListener("click", () => {
      let value = button.dataset.terminalAction
        ? terminalActionValue(button.dataset.terminalAction)
        : decodeKey(button.dataset.terminalKey || "");
      if (!value) return;
      if (ctrlArmed && value.length === 1) {
        value = String.fromCharCode(value.toUpperCase().charCodeAt(0) & 31);
        ctrlArmed = false;
        ctrlButton?.setAttribute("aria-pressed", "false");
      }
      term.input(value, true);
      if (button.closest(".more-keys-panel")) {
        closeMoreKeys();
      } else {
        term.focus();
      }
    });
  });

  document.querySelector("#close-more-keys")?.addEventListener("click", () => closeMoreKeys());
  document.querySelector("#focus-terminal")?.addEventListener("click", () => closeMoreKeys());
  document.querySelector("#paste-terminal")?.addEventListener("click", async () => {
    closeMoreKeys({ focus: false });
    if (!window.isSecureContext || !navigator.clipboard?.readText) {
      setStatus(tr("terminal.status.clipboard_insecure"));
      term.focus();
      return;
    }
    try {
      const text = await navigator.clipboard.readText();
      // Let xterm apply bracketed-paste semantics instead of bypassing the
      // terminal input pipeline with a raw WebSocket write.
      term.paste(text);
      term.focus();
    } catch {
      setStatus(tr("terminal.status.clipboard"));
      term.focus();
    }
  });

  const commandForm = document.querySelector("#command-form");
  const commandInput = document.querySelector("#command-input");
  const commandMeta = document.querySelector("#command-meta");
  let composerComposing = false;
  commandInput?.addEventListener("compositionstart", () => {
    composerComposing = true;
  });
  commandInput?.addEventListener("compositionend", () => {
    composerComposing = false;
  });
  commandInput?.addEventListener("focus", updateMobileKeyboardState);
  commandInput?.addEventListener("blur", () => window.setTimeout(updateMobileKeyboardState, 0));
  const updateCommandComposer = () => {
    if (!commandInput) return;
    commandInput.style.height = "auto";
    commandInput.style.height = `${Math.min(commandInput.scrollHeight, 132)}px`;
    const lines = commandInput.value ? commandInput.value.split("\n").length : 0;
    if (commandMeta) {
      commandMeta.textContent =
        lines > 1
          ? tr("terminal.command_multiline", { lines })
          : tr("terminal.command_hint");
    }
  };
  commandForm?.addEventListener("submit", (event) => {
    event.preventDefault();
    if (composerComposing) return;
    if (!commandInput.value.trim()) return;
    send({ kind: "command", data: commandInput.value });
    commandInput.value = "";
    updateCommandComposer();
    commandInput.blur();
    setComposerOpen(false, { focus: false });
  });
  commandInput?.addEventListener("input", updateCommandComposer);
  commandInput?.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      commandForm?.requestSubmit();
    }
  });
  updateCommandComposer();

  document.querySelectorAll("[data-command-reuse]").forEach((button) => {
    button.addEventListener("click", () => {
      setComposerOpen(true, { focus: false });
      commandInput.value = button.dataset.commandReuse || "";
      updateCommandComposer();
      commandInput.focus();
      commandInput.setSelectionRange(commandInput.value.length, commandInput.value.length);
    });
  });

  connect();
})();
