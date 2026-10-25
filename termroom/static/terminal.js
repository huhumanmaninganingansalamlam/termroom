(async () => {
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
  const screenReaderModeToggle = document.querySelector("#terminal-screen-reader-mode");
  if (screenReaderModeToggle) {
    screenReaderModeToggle.checked = false;
  }

  const TERMINAL_FONT_SIZE_KEY = "termroom.terminal.font-size";
  const DEFAULT_TERMINAL_FONT_SIZE = 14;
  const MIN_TERMINAL_FONT_SIZE = 12;
  const MAX_TERMINAL_FONT_SIZE = 20;
  // Programs can emit palette indices or fixed RGB colors that nearly match
  // either the light or dark terminal background. Let xterm preserve the hue
  // while lifting only low-contrast foregrounds to normal-text readability.
  // Its theme service clears this contrast cache whenever the theme changes.
  const MINIMUM_TERMINAL_CONTRAST_RATIO = 4.5;
  const TERMINAL_CELL_WIDTH_PROPERTY = "--termroom-terminal-cell-width";
  const TERMINAL_CELL_HEIGHT_PROPERTY = "--termroom-terminal-cell-height";
  const KOREAN_TERMINAL_FONT_FAMILY = '"Termroom Korean Terminal"';
  const BUNDLED_TERMINAL_FONT_FAMILY = '"Termroom D2Koding Nerd Mono"';
  const BUNDLED_TERMINAL_FONT_PROBE = "M";
  // Let fast cached/local loads avoid a font swap without holding terminal input
  // behind a slow first-visit font download.
  const BUNDLED_TERMINAL_FONT_LOAD_TIMEOUT_MS = 400;
  const BUNDLED_TERMINAL_FONT_DESCRIPTOR =
    `normal 400 ${DEFAULT_TERMINAL_FONT_SIZE}px ${BUNDLED_TERMINAL_FONT_FAMILY}`;
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

  const systemTerminalFontFamily = () => {
    const configured = window
      .getComputedStyle(document.documentElement)
      .getPropertyValue("--font-mono")
      .trim();
    return (
      configured ||
      'ui-monospace, "SFMono-Regular", Menlo, Monaco, Consolas, "Cascadia Mono", "Segoe UI Mono", "Noto Sans Mono CJK KR", D2Coding, "Nanum Gothic Coding", "Apple SD Gothic Neo", "Malgun Gothic", "Noto Sans CJK KR", "Noto Sans KR", "Liberation Mono", monospace'
    );
  };
  const terminalFontFamily = (includeBundledFont) => {
    const systemFamily = systemTerminalFontFamily();
    return includeBundledFont
      ? `${KOREAN_TERMINAL_FONT_FAMILY}, ${BUNDLED_TERMINAL_FONT_FAMILY}, ${systemFamily}`
      : `${KOREAN_TERMINAL_FONT_FAMILY}, ${systemFamily}`;
  };

  const beginBundledTerminalFontLoad = () => {
    if (typeof document.fonts?.load !== "function") {
      const unavailable = Promise.resolve(false);
      return { initial: unavailable, completed: unavailable };
    }
    const completed = document.fonts
      .load(BUNDLED_TERMINAL_FONT_DESCRIPTOR, BUNDLED_TERMINAL_FONT_PROBE)
      .then((faces) => faces.length > 0)
      .catch(() => false);
    const initial = (async () => {
      let timeoutId;
      const timeout = new Promise((resolve) => {
        timeoutId = window.setTimeout(
          () => resolve(false),
          BUNDLED_TERMINAL_FONT_LOAD_TIMEOUT_MS,
        );
      });
      const loaded = await Promise.race([completed, timeout]);
      window.clearTimeout(timeoutId);
      return loaded;
    })();
    return { initial, completed };
  };

  const status = document.querySelector("#terminal-status");
  const statusLabel = status.querySelector(".terminal-status-label");
  const reconnectButton = document.querySelector("#terminal-reconnect");
  const signInLink = document.querySelector("#terminal-sign-in");
  const inputError = document.querySelector("#terminal-input-error");
  const setInputError = (message = "") => {
    if (!inputError) return;
    inputError.textContent = message;
    inputError.hidden = !message;
  };
  const bundledTerminalFontLoad = beginBundledTerminalFontLoad();
  let bundledTerminalFontLoaded = await bundledTerminalFontLoad.initial;
  host.dataset.terminalFont = bundledTerminalFontLoaded ? "bundled" : "system-fallback";
  const Unicode11AddonClass = window.Unicode11Addon?.Unicode11Addon;
  const unicode11Available = typeof Unicode11AddonClass === "function";
  const term = new window.Terminal({
    cursorBlink: true,
    cursorStyle: "bar",
    fontFamily: terminalFontFamily(bundledTerminalFontLoaded),
    fontSize: initialTerminalFontSize,
    lineHeight: 1.2,
    minimumContrastRatio: MINIMUM_TERMINAL_CONTRAST_RATIO,
    scrollback: 5000,
    screenReaderMode: false,
    allowProposedApi: unicode11Available,
    theme: terminalTheme(),
  });
  host.dataset.terminalUnicode = "default";
  if (unicode11Available) {
    try {
      const unicode11Addon = new Unicode11AddonClass();
      term.loadAddon(unicode11Addon);
      term.unicode.activeVersion = "11";
      host.dataset.terminalUnicode = "11";
    } catch {
      // Keep the terminal usable if an asset is stale or incompatible. The
      // pinned asset and static contract make this a recovery path, not the
      // normal runtime configuration.
    }
  }
  term.open(host);
  Object.defineProperty(host, "termroomActiveBufferType", {
    configurable: true,
    get: () => term.buffer.active.type,
  });
  let paneMode = null;
  let paneModeGeneration = null;
  let paneModeRevision = 0;
  Object.defineProperty(host, "termroomPaneMode", {
    configurable: true,
    get: () => paneMode,
  });
  const acceptPaneMode = (value) => {
    if (
      host.dataset.paneModeCapable !== "true"
      || value?.kind !== "pane_mode"
      || value.terminal_id !== host.dataset.terminalId
      || typeof value.generation !== "string"
      || !Number.isSafeInteger(value.revision)
      || value.revision < 1
      || (value.available !== undefined && typeof value.available !== "boolean")
      || (value.in_mode !== undefined && typeof value.in_mode !== "boolean")
      || (value.available !== false
        && (typeof value.alternate !== "boolean"
          || typeof value.mouse_tracking !== "boolean"))
      || (paneModeGeneration !== null && value.generation !== paneModeGeneration)
      || (value.generation === paneModeGeneration && value.revision <= paneModeRevision)
    ) return;
    paneModeGeneration = value.generation;
    paneModeRevision = value.revision;
    paneMode = value.available === false ? null : Object.freeze(value);
    host.dataset.inputTargetPane = paneMode?.pane || "";
    host.dataset.inputTargetState = !paneMode ? "unknown" : paneMode.dead ? "ended"
      : paneMode.in_mode ? "tmux_mode" : "available";
    host.dispatchEvent(new Event("termroom:pane-mode"));
    updateCommandComposer();
  };
  // A TUI can leave mouse modes enabled when it exits. tmux then forwards
  // reports to readline, which inserts their numeric suffix into shell input.
  // Gate xterm's mouse event source, not onData: pasted/user text stays intact.
  const mouseService = term._core.coreMouseService;
  const reportMouse = mouseService.triggerMouseEvent.bind(mouseService);
  mouseService.triggerMouseEvent = (event) => {
    // An ended shell cannot consume reports; tmux still needs pane-selection clicks.
    if (paneMode?.shell_foreground === true && !paneMode.alternate && !paneMode.dead) return false;
    return reportMouse(event);
  };
  term.options.screenReaderMode = false;
  const syncAccessibleTerminalRows = () => {
    // Expose the rendered viewport without copying text or enabling spoken output.
    const rows = host.querySelector(".xterm-rows");
    rows?.setAttribute("role", "group");
    rows?.setAttribute("aria-label", tr("terminal.visible_rows"));
    rows?.setAttribute("aria-hidden", String(term.options.screenReaderMode));
    host.querySelector(".xterm-accessibility-tree")?.setAttribute("aria-label", tr("terminal.visible_rows"));
  };
  syncAccessibleTerminalRows();
  screenReaderModeToggle?.addEventListener("change", () => {
    term.options.screenReaderMode = screenReaderModeToggle.checked;
    syncAccessibleTerminalRows();
  });
  const terminalStringCellWidth = (value) => {
    const service = term._core?.unicodeService;
    if (typeof service?.getStringCellWidth !== "function") return null;
    try {
      const width = Number(service.getStringCellWidth(String(value || "")));
      return Number.isFinite(width) && width >= 0 ? width : null;
    } catch {
      return null;
    }
  };
  Object.defineProperty(host, "termroomStringCellWidth", {
    configurable: true,
    value: terminalStringCellWidth,
  });
  let terminalMetricSignature = "";
  const invalidateTerminalMetrics = () => {
    terminalMetricSignature = "";
  };
  const publishTerminalMetrics = (cell) => {
    const width = Number(cell?.width);
    const height = Number(cell?.height);
    if (
      !Number.isFinite(width)
      || width <= 0
      || !Number.isFinite(height)
      || height <= 0
    ) return;
    host.style.setProperty(TERMINAL_CELL_WIDTH_PROPERTY, `${width}px`);
    host.style.setProperty(TERMINAL_CELL_HEIGHT_PROPERTY, `${height}px`);
    const signature = [
      width.toFixed(4),
      height.toFixed(4),
      String(term.options.fontFamily || ""),
      String(term.options.fontSize || ""),
    ].join("|");
    if (signature === terminalMetricSignature) return;
    terminalMetricSignature = signature;
    window.dispatchEvent(
      new CustomEvent("termroom:terminal-metrics", {
        detail: { cellWidth: width, cellHeight: height },
      }),
    );
  };
  const refreshTerminalFont = () => {
    term.clearTextureAtlas?.();
    term.refresh(0, Math.max(0, term.rows - 1));
  };
  document.fonts?.addEventListener?.("loadingdone", () => {
    invalidateTerminalMetrics();
    refreshTerminalFont();
    scheduleResize(true);
  });
  void bundledTerminalFontLoad.completed.then((loaded) => {
    if (!loaded || bundledTerminalFontLoaded) return;
    bundledTerminalFontLoaded = true;
    host.dataset.terminalFont = "bundled";
    term.options.fontFamily = terminalFontFamily(true);
    invalidateTerminalMetrics();
    refreshTerminalFont();
    scheduleResize(true);
  });
  window.addEventListener("themechange", () => {
    term.options.theme = terminalTheme();
    term.options.fontFamily = terminalFontFamily(bundledTerminalFontLoaded);
    term.refresh(0, Math.max(0, term.rows - 1));
    scheduleResize(true);
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
  let reconnectAllowed = true;
  let connectionEpoch = 0;
  let pendingTerminalId = "";
  Object.defineProperty(host, "termroomConnectionEpoch", {
    get: () => connectionEpoch,
  });
  let isConnected = false;
  let initialOutputParsed = false;
  let lastInputRevision = 0;
  let presenceInitialized = false;
  let presenceRequest = null;
  let otherInputTimer = null;
  let shellTerminal = (host.dataset.terminalRole || "shell") === "shell";
  let activityAckTimer = 0;
  let pendingActivityAt = 0;
  let acknowledgedActivityAt = 0;
  let renderedActivityAt = 0;
  let outputRenderSequence = 0;
  let acknowledgedRenderSequence = 0;
  const mobileInput = window.matchMedia("(max-width: 1023px)");
  const coarsePrimaryPointer = window.matchMedia("(pointer: coarse)");
  const composePane = document.querySelector(".terminal-compose-pane");
  const composerRoot = document.querySelector(".terminal-composer");
  const openCommandEditor = document.querySelector("#open-command-editor");
  const closeCommandEditor = document.querySelector("#close-command-editor");
  const moreKeys = document.querySelector(".more-keys[data-popover]");
  let composerOpen = false;
  let mobileViewportBaseline = window.visualViewport?.height || window.innerHeight;
  let mobileViewportBaselineWidth = window.visualViewport?.width || window.innerWidth;

  let controlGeneration = null;
  let controlRevision = 0;
  let controlsInput = false;
  let controlClaimPending = false;
  let controlClaimId = 0;
  let activationPending = document.visibilityState === "visible" && document.hasFocus();
  let programmaticTerminalFocus = false;
  const pendingControlInput = [];
  let pendingControlBytes = 0;
  const MAX_PENDING_CONTROL_BYTES = 1024 * 1024;
  Object.defineProperty(host, "termroomControlsInput", { get: () => controlsInput });
  const updateInputControl = () => {
    term.options.disableStdin = !controlsInput && !controlClaimPending;
  };
  const acceptInputControl = (value) => {
    if (
      value?.terminal_id !== host.dataset.terminalId ||
      typeof value.generation !== "string" ||
      typeof value.controller_client_id !== "string" ||
      !Number.isSafeInteger(value.control_revision) ||
      (controlGeneration !== null && value.generation !== controlGeneration)
    ) return;
    if (value.control_revision >= controlRevision) {
      controlGeneration = value.generation;
      controlRevision = value.control_revision;
      controlsInput = value.controller_client_id === controlGeneration;
    }
    if (value.kind === "terminal_control" && value.claim_id === controlClaimId) {
      controlClaimPending = false;
      const queued = pendingControlInput.splice(0);
      pendingControlBytes = 0;
      if (controlsInput && socket?.readyState === WebSocket.OPEN) {
        queued.forEach(({ frame }) => socket.send(frame));
      } else if (queued.some(({ command }) => typeof command === "string")) {
        resetCommandDelivery();
      }
    }
    updateInputControl();
  };
  const resetInputControl = () => {
    controlGeneration = null;
    controlRevision = 0;
    controlsInput = false;
    controlClaimPending = false;
    pendingControlInput.length = 0;
    pendingControlBytes = 0;
    resetCommandDelivery();
    updateInputControl();
  };
  const claimInputControl = () => {
    if (programmaticTerminalFocus || document.visibilityState !== "visible" || !document.hasFocus()) return;
    if (controlClaimPending) return;
    activationPending = true;
    if (socket?.readyState !== WebSocket.OPEN) return;
    activationPending = false;
    controlClaimPending = true;
    updateInputControl();
    socket.send(JSON.stringify({ kind: "control_claim", claim_id: ++controlClaimId }));
  };
  const sendInputFrame = (frame, command) => {
    if (socket?.readyState !== WebSocket.OPEN) return false;
    if (controlClaimPending) {
      const bytes = typeof frame === "string" ? new TextEncoder().encode(frame).byteLength : frame.byteLength;
      if (pendingControlBytes + bytes > MAX_PENDING_CONTROL_BYTES) {
        socket.close(1009, "Terminal input is too large");
        return false;
      }
      if (typeof command === "string" && pendingControlInput.some((entry) => entry.command === command)) return false;
      pendingControlInput.push({ frame, command });
      pendingControlBytes += bytes;
      // Enqueue is not delivery: only a matching server command ACK clears the draft.
      return true;
    }
    if (!controlsInput) return false;
    socket.send(frame);
    return true;
  };
  updateInputControl();
  window.addEventListener("focus", claimInputControl);
  document.addEventListener("pointerdown", (event) => {
    if (event.target.closest?.("#terminal, .terminal-composer, [data-terminal-switch]")) claimInputControl();
  }, true);
  document.addEventListener("focusin", (event) => {
    if (event.target.closest?.("#terminal, .terminal-composer")) claimInputControl();
  });

  const setStatusMessage = (message) => {
    statusLabel.textContent = message;
    status.setAttribute("aria-label", message);
  };

  const setStatus = (message, connected = false) => {
    isConnected = connected;
    host.dataset.terminalReady = String(connected && initialOutputParsed);
    setStatusMessage(message);
    status.classList.toggle("connected", connected);
    updateCommandComposer();
    if (connected) {
      if (reconnectButton) reconnectButton.hidden = true;
      if (signInLink) signInLink.hidden = true;
    }
  };

  const cancelPresenceRequest = () => {
    presenceRequest?.controller.abort();
    presenceRequest = null;
  };

  const updatePresence = async () => {
    if (
      !isConnected ||
      document.visibilityState === "hidden" ||
      presenceRequest
    ) return;
    const request = { controller: new AbortController() };
    presenceRequest = request;
    const terminalId = host.dataset.terminalId;
    const epoch = connectionEpoch;
    try {
      const response = await fetch(`/api/terminals/${terminalId}/presence`, {
        credentials: "same-origin",
        cache: "no-store",
        signal: request.controller.signal,
      });
      if (!response.ok) return;
      const presence = await response.json();
      if (
        presenceRequest !== request ||
        request.controller.signal.aborted ||
        epoch !== connectionEpoch ||
        terminalId !== host.dataset.terminalId
      ) return;
      if (controlGeneration !== null) {
        acceptInputControl({ ...presence, generation: controlGeneration, terminal_id: terminalId });
      }
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
    } finally {
      if (presenceRequest === request) presenceRequest = null;
    }
  };

  const send = (payload) => {
    if (socket?.readyState !== WebSocket.OPEN) return false;
    const frame = JSON.stringify(payload);
    if (payload.kind === "input" || payload.kind === "command") {
      if (controlClaimPending && payload.kind === "input" && payload.user_input !== true) return false;
      return sendInputFrame(frame, payload.kind === "command" ? payload.data : undefined);
    }
    socket.send(frame);
    return true;
  };

  const terminalAtBottom = () =>
    term.buffer.active.viewportY >= term.buffer.active.baseY;
  const terminalCanAcknowledge = () =>
    shellTerminal
    && document.visibilityState === "visible"
    && document.hasFocus()
    && document.querySelector(".terminal-scroll-live")?.hidden !== false
    && !document.body.classList.contains("terminal-scroll-native-selection")
    && terminalAtBottom();
  const terminalActivityPayload = (result) => {
    const payload = result?.data && typeof result.data === "object" ? result.data : result;
    if (payload?.terminal && typeof payload.terminal === "object") return payload.terminal;
    if (Array.isArray(payload?.terminals)) {
      return payload.terminals.find(
        (item) => String(item?.terminal_id ?? item?.id ?? "") === host.dataset.terminalId,
      ) || payload.terminals[0] || {};
    }
    return payload;
  };
  const renderCurrentTerminalActivity = (result) => {
    const payload = terminalActivityPayload(result);
    const activityAt = Number(payload?.activity_at || 0);
    const acknowledgedAt = Number(payload?.acknowledged_activity_at || 0);
    if (Number.isSafeInteger(activityAt)) {
      pendingActivityAt = Math.max(pendingActivityAt, activityAt);
    }
    if (Number.isSafeInteger(acknowledgedAt)) {
      acknowledgedActivityAt = Math.max(acknowledgedActivityAt, acknowledgedAt);
    }
    if (pendingActivityAt <= acknowledgedActivityAt) {
      acknowledgedRenderSequence = outputRenderSequence;
    } else if (outputRenderSequence > acknowledgedRenderSequence) {
      renderedActivityAt = Math.max(renderedActivityAt, pendingActivityAt);
    }
    document
      .querySelectorAll(`[data-terminal-activity-tab="${CSS.escape(host.dataset.terminalId)}"] [data-terminal-activity-tab-unread]`)
      .forEach((dot) => {
        dot.hidden = pendingActivityAt <= acknowledgedActivityAt;
      });
    scheduleActivityAcknowledge();
  };
  const acknowledgeVisibleActivity = () => {
    window.clearTimeout(activityAckTimer);
    if (
      !terminalCanAcknowledge()
      || renderedActivityAt <= acknowledgedActivityAt
    ) return;
    const observedActivityAt = renderedActivityAt;
    if (!send({ kind: "activity_ack", activity_at: observedActivityAt })) return;
    acknowledgedActivityAt = observedActivityAt;
    acknowledgedRenderSequence = outputRenderSequence;
    renderCurrentTerminalActivity({
      activity_at: pendingActivityAt,
      acknowledged_activity_at: acknowledgedActivityAt,
      unread: pendingActivityAt > acknowledgedActivityAt,
    });
    window.dispatchEvent(
      new CustomEvent("termroom:terminal-activity-changed", {
        detail: {
          workspace_id: host.dataset.workspaceId,
          terminal_id: host.dataset.terminalId,
        },
      }),
    );
  };
  const scheduleActivityAcknowledge = () => {
    window.clearTimeout(activityAckTimer);
    if (terminalCanAcknowledge()) {
      activityAckTimer = window.setTimeout(acknowledgeVisibleActivity, 180);
    }
  };

  const resizeTerminal = (forceSend = false) => {
    // A hidden host has no viewport; clamping it would erase alternate-screen rows.
    if (host.clientWidth <= 0 || host.clientHeight <= 0) return;
    const cell = term._core?._renderService?.dimensions?.css?.cell;
    if (!cell?.width || !cell?.height) return;
    publishTerminalMetrics(cell);

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
      // xterm 6 skips shrinking an empty alternate buffer's capacity. If the
      // editor opens before SSH output, that spare capacity becomes scrollback
      // and later TUI redraws scroll the visible response away. An empty
      // alternate buffer has no content to preserve and must fit this viewport.
      const alternate = term._core._bufferService.buffers.alt;
      if (alternate.lines.length === 0) alternate.lines.maxLength = rows;
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
        // Storage is optional; the local font size still applies.
      }
    }
    scheduleResize(true);
  };
  updateFontSizeControls(initialTerminalFontSize);
  fontSizeDecrease?.addEventListener("click", () =>
    applyTerminalFontSize(Number(term.options.fontSize) - 1),
  );
  fontSizeIncrease?.addEventListener("click", () =>
    applyTerminalFontSize(Number(term.options.fontSize) + 1),
  );
  fontSizeReset?.addEventListener("click", () =>
    applyTerminalFontSize(DEFAULT_TERMINAL_FONT_SIZE),
  );
  window.addEventListener("storage", (event) => {
    if (event.key === TERMINAL_FONT_SIZE_KEY) {
      applyTerminalFontSize(event.newValue || DEFAULT_TERMINAL_FONT_SIZE, { persist: false });
    }
  });

  const setComposerOpen = (open, { focus = true } = {}) => {
    composerOpen = Boolean(open);
    openCommandEditor?.setAttribute("aria-expanded", String(composerOpen));
    composerRoot?.classList.toggle("command-editor-open", composerOpen);
    if (composePane) composePane.hidden = !composerOpen;
    if (focus) {
      if (composerOpen) {
        document.querySelector("#command-input")?.focus();
      } else {
        term.focus();
      }
    }
    window.setTimeout(() => scheduleResize(), 0);
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
    window.setTimeout(() => scheduleResize(), 0);
  };

  openCommandEditor?.addEventListener("click", () => setComposerOpen(true));
  closeCommandEditor?.addEventListener("click", () => setComposerOpen(false));
  terminalTextarea?.addEventListener("focus", () => {
    if (mobileInput.matches && composerOpen) {
      setComposerOpen(false, { focus: false });
    }
  });
  setComposerOpen(false, { focus: false });

  const connect = () => {
    window.clearTimeout(reconnectTimer);
    cancelPresenceRequest();
    resetInputControl();
    const previousSocket = socket;
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const epoch = ++connectionEpoch;
    const terminalId = host.dataset.terminalId;
    const nextSocket = new WebSocket(
      `${scheme}://${location.host}/ws/terminal/${terminalId}`,
    );
    nextSocket.binaryType = "arraybuffer";
    paneMode = null;
    paneModeGeneration = null;
    paneModeRevision = 0;
    host.dataset.inputTargetPane = "";
    host.dataset.inputTargetState = "unknown";
    host.dispatchEvent(new Event("termroom:pane-mode"));
    socket = nextSocket;
    if (previousSocket && previousSocket.readyState < WebSocket.CLOSING) {
      previousSocket.close(1000, "reconnect");
    }
    if (reconnectButton) reconnectButton.disabled = true;
    if (signInLink) signInLink.hidden = true;
    initialOutputParsed = false;
    host.dataset.terminalReady = "false";
    host.dataset.outputFrames = "0";
    host.dataset.parsedFrames = "0";
    setStatus(tr("terminal.status.connecting"));

    const completeInitialScreen = () => {
      if (!paneMode || !initialOutputParsed || isConnected) return;
      host.dataset.terminalReady = "true";
      setStatus(tr("terminal.status.connected"), true);
      updatePresence();
    };

    nextSocket.addEventListener("open", () => {
      if (epoch !== connectionEpoch || nextSocket !== socket) {
        nextSocket.close(1000, "terminal switched");
        return;
      }
      reconnectDelay = 500;
      scheduleResize(true);
      if (activationPending) claimInputControl();
      if (!coarsePrimaryPointer.matches) {
        programmaticTerminalFocus = true;
        try { term.focus(); } finally { programmaticTerminalFocus = false; }
      }
    });
    nextSocket.addEventListener("message", (event) => {
      if (epoch !== connectionEpoch || nextSocket !== socket) return;
      if (event.data instanceof ArrayBuffer) {
        try {
          const previousPaneMode = paneMode;
          const control = JSON.parse(new TextDecoder().decode(event.data));
          if (control.kind === "terminal_control" || control.kind === "pane_mode") acceptInputControl(control);
          if (control.kind === "command_ack") acceptCommandDelivery(control);
          acceptPaneMode(control);
          // A current successful pane query supersedes the page-load SSH failure.
          // Transport open, arbitrary output, and unrelated errors are not recovery.
          if (paneMode && paneMode !== previousPaneMode) {
            document.querySelector("[data-terminal-connection-error]")?.remove();
            if (!initialOutputParsed) {
              setStatus(tr("terminal.status.waiting_output"));
              if (reconnectButton) {
                reconnectButton.hidden = false;
                reconnectButton.disabled = false;
              }
            }
            completeInitialScreen();
          }
        } catch {
          // Invalid controls are never rendered or forwarded as terminal input.
        }
        return;
      }
      if (typeof event.data !== "string") return;
      host.dataset.outputFrames = String(Number(host.dataset.outputFrames) + 1);
      term.write(event.data, () => {
        if (epoch !== connectionEpoch || nextSocket !== socket) return;
        host.dataset.parsedFrames = String(Number(host.dataset.parsedFrames) + 1);
        if (!initialOutputParsed) {
          initialOutputParsed = true;
          updateCommandComposer();
        }
        completeInitialScreen();
        outputRenderSequence += 1;
        if (pendingActivityAt > acknowledgedActivityAt) {
          renderedActivityAt = Math.max(renderedActivityAt, pendingActivityAt);
        }
        scheduleActivityAcknowledge();
        window.dispatchEvent(
          new CustomEvent("termroom:terminal-output", {
            detail: {
              workspace_id: host.dataset.workspaceId,
              terminal_id: host.dataset.terminalId,
            },
          }),
        );
      });
    });
    nextSocket.addEventListener("close", (event) => {
      if (epoch !== connectionEpoch || nextSocket !== socket) return;
      cancelPresenceRequest();
      resetInputControl();
      const terminalCloseMessages = {
        4401: tr("terminal.status.auth_required"),
        4403: tr("terminal.status.rejected"),
        4404: tr("terminal.status.not_found"),
        4429: tr("terminal.status.too_many"),
      };
      const terminalCloseMessage = terminalCloseMessages[event.code];
      reconnectAllowed = !terminalCloseMessage;
      setStatus(terminalCloseMessage || tr("terminal.status.reconnecting"));
      if (reconnectButton) {
        reconnectButton.hidden = !reconnectAllowed;
        reconnectButton.disabled = false;
      }
      if (signInLink) signInLink.hidden = event.code !== 4401;
      if (reconnectAllowed && document.visibilityState !== "hidden") {
        reconnectTimer = window.setTimeout(connect, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 1.7, 5000);
      }
    });
    nextSocket.addEventListener("error", () => nextSocket.close());
  };
  reconnectButton?.addEventListener("click", () => {
    if (reconnectAllowed) {
      activationPending = true;
      connect();
    }
  });
  window.addEventListener("offline", () => {
    if (!reconnectAllowed) return;
    // TCP close can lag network loss. Retire this generation immediately so
    // buffered frames cannot re-establish readiness or confirm an old command.
    connectionEpoch += 1;
    window.clearTimeout(reconnectTimer);
    cancelPresenceRequest();
    resetInputControl();
    initialOutputParsed = false;
    const previousSocket = socket;
    socket = null;
    setStatus(tr("terminal.status.offline"));
    if (reconnectButton) {
      reconnectButton.hidden = false;
      reconnectButton.disabled = false;
    }
    if (previousSocket && previousSocket.readyState < WebSocket.CLOSING) {
      previousSocket.close(1000, "offline");
    }
  });
  window.addEventListener("online", () => {
    if (reconnectAllowed && !socket && document.visibilityState !== "hidden") connect();
  });

  let nextTerminalDataIsUserInput = false;
  const onUserInput = term._core?.coreService?.onUserInput;
  const hasUserInputSignal = typeof onUserInput === "function";
  if (hasUserInputSignal) {
    onUserInput(() => {
      nextTerminalDataIsUserInput = true;
    });
  }
  let pastedCommand = null;
  let pastedCommandSent = false;
  let commandPending = false;
  let pendingCommand = null;
  let commandSequence = 0;
  let pendingCommandPaste = null;
  const pastePendingCommand = () => {
    if (!initialOutputParsed || !pendingCommandPaste) return;
    const paste = pendingCommandPaste;
    pendingCommandPaste = null;
    // Socket/control readiness does not initialize the application's paste mode.
    term.write("", paste);
  };
  term.onWriteParsed(pastePendingCommand);
  term.onData((data) => {
    const userInput = hasUserInputSignal && nextTerminalDataIsUserInput;
    nextTerminalDataIsUserInput = false;
    if (pastedCommand !== null) {
      pastedCommandSent = send({
        kind: "command",
        command_id: pendingCommand?.id,
        pane: pendingCommand?.pane,
        data: pastedCommand,
        paste_data: data,
        rows: term.rows,
        cols: term.cols,
      });
      return;
    }
    send({
      kind: "input",
      data,
      rows: term.rows,
      cols: term.cols,
      user_input: userInput,
    });
  });
  term.onBinary((data) => {
    if (socket?.readyState !== WebSocket.OPEN) return;
    sendInputFrame(Uint8Array.from(data, (character) => character.charCodeAt(0)));
  });
  term.onScroll(scheduleActivityAcknowledge);
  window.addEventListener(
    "termroom:terminal-activity-refreshed",
    (event) => renderCurrentTerminalActivity(event.detail),
  );
  new ResizeObserver(() => scheduleResize()).observe(host);
  window.visualViewport?.addEventListener("resize", () => {
    updateMobileKeyboardState();
    window.setTimeout(() => scheduleResize(), 50);
  });
  terminalTextarea?.addEventListener("focus", updateMobileKeyboardState);
  terminalTextarea?.addEventListener("blur", () => window.setTimeout(updateMobileKeyboardState, 0));
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") {
      cancelPresenceRequest();
      return;
    }
    if (document.hasFocus()) claimInputControl();
    if (reconnectAllowed && socket?.readyState === WebSocket.CLOSED) {
      connect();
      return;
    }
    updatePresence();
    scheduleActivityAcknowledge();
  });
  window.addEventListener("focus", () => {
    if (reconnectAllowed && socket?.readyState === WebSocket.CLOSED) {
      connect();
      return;
    }
    scheduleActivityAcknowledge();
  });
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

  const terminalActionValue = (action, { ctrl = false } = {}) => {
    if (ctrl) {
      switch (action) {
        case "arrow-up":
          return "\u001b[1;5A";
        case "arrow-down":
          return "\u001b[1;5B";
        case "arrow-right":
          return "\u001b[1;5C";
        case "arrow-left":
          return "\u001b[1;5D";
        default:
          return "";
      }
    }
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

  const terminalCtrlValue = (value) => {
    if (!value || value.length !== 1) return "";
    const code = value.charCodeAt(0);
    if (code < 32 || code === 127) return value;
    if (/^[A-Za-z]$/.test(value)) {
      return String.fromCharCode(value.toUpperCase().charCodeAt(0) & 31);
    }
    if (value === "_") return "\u001f";
    return "";
  };

  let ctrlArmed = false;
  const ctrlButton = document.querySelector("#ctrl-key");
  const closeMoreKeys = ({ focus = true } = {}) => {
    if (moreKeys) {
      moreKeys.removeAttribute("open");
      moreKeys.querySelector("[data-popover-trigger]")?.setAttribute("aria-expanded", "false");
      moreKeys.querySelectorAll("[data-popover-panel]").forEach((panel) => {
        panel.hidden = true;
      });
    }
    if (focus) term.focus();
  };
  ctrlButton?.addEventListener("click", () => {
    ctrlArmed = !ctrlArmed;
    ctrlButton.setAttribute("aria-pressed", String(ctrlArmed));
    term.focus();
  });

  document.querySelectorAll("[data-terminal-key], [data-terminal-action]").forEach((button) => {
    button.addEventListener("click", () => {
      const action = button.dataset.terminalAction || "";
      let value = action
        ? terminalActionValue(action, { ctrl: ctrlArmed })
        : decodeKey(button.dataset.terminalKey || "");
      if (ctrlArmed) {
        if (!action) value = terminalCtrlValue(value);
        ctrlArmed = false;
        ctrlButton?.setAttribute("aria-pressed", "false");
      }
      if (value) term.input(value, true);
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
    setInputError();
    if (!window.isSecureContext || !navigator.clipboard?.readText) {
      setInputError(tr("terminal.status.clipboard_insecure"));
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
      setInputError(tr("terminal.status.clipboard"));
      term.focus();
    }
  });

  const commandForm = document.querySelector("#command-form");
  const commandInput = document.querySelector("#command-input");
  const commandMeta = document.querySelector("#command-meta");
  let commandUnconfirmed = false;
  const commandDelivery = document.querySelector("#command-delivery");
  const setCommandDelivery = (state) => {
    if (!commandDelivery) return;
    commandDelivery.dataset.state = state;
    commandDelivery.hidden = state === "idle";
    const label = commandDelivery.querySelector(".terminal-status-label");
    const message = state === "idle" ? "" : tr(`terminal.command_delivery.${state}`);
    label.textContent = message;
    commandDelivery.title = message;
  };
  const commandDraftKey = () => `termroom:command-draft:${host.dataset.workspaceId}:${host.dataset.terminalId}`;
  const persistCommandDraft = () => {
    if (!commandInput) return;
    try {
      if (commandInput.value) {
        sessionStorage.setItem(commandDraftKey(), JSON.stringify({
          value: commandInput.value, unconfirmed: commandUnconfirmed,
        }));
      } else {
        sessionStorage.removeItem(commandDraftKey());
      }
    } catch {
      // Storage can be unavailable; the live draft still waits for server ACK.
    }
  };
  const restoreCommandDraft = () => {
    if (!commandInput) return;
    commandInput.value = "";
    commandUnconfirmed = false;
    try {
      const draft = JSON.parse(sessionStorage.getItem(commandDraftKey()));
      if (typeof draft?.value === "string") {
        commandInput.value = draft.value;
        commandUnconfirmed = draft.unconfirmed === true;
      }
    } catch {
      // A malformed or unavailable draft never becomes terminal input.
    }
    setCommandDelivery(commandUnconfirmed ? "unconfirmed" : "idle");
    updateCommandComposer();
  };
  const resetCommandDelivery = () => {
    if (commandPending) setCommandDelivery("unconfirmed");
    commandPending = false;
    pendingCommand = null;
    pendingCommandPaste = null;
    updateCommandComposer();
  };
  const acceptCommandDelivery = (value) => {
    if (
      !pendingCommand || value.terminal_id !== host.dataset.terminalId ||
      value.generation !== controlGeneration || value.command_id !== pendingCommand.id ||
      typeof value.accepted !== "boolean"
    ) return;
    const command = pendingCommand.command;
    resetCommandDelivery();
    commandUnconfirmed = false;
    if (value.accepted) rememberCommand(command);
    setCommandDelivery(commandInput.value === command
      ? value.accepted ? "accepted"
        : ["pane_ended", "pane_changed", "pane_unavailable", "pane_in_mode"].includes(value.reason)
          ? value.reason : "rejected"
      : "idle");
    if (value.accepted && commandInput.value === command) {
      commandInput.value = "";
      commandInput.blur();
      setComposerOpen(false, { focus: false });
    }
    persistCommandDraft();
    updateCommandComposer();
  };
  window.addEventListener("pagehide", persistCommandDraft);
  let composerComposing = false;
  commandInput?.addEventListener("compositionstart", () => {
    composerComposing = true;
  });
  commandInput?.addEventListener("compositionend", () => {
    composerComposing = false;
  });
  commandInput?.addEventListener("focus", updateMobileKeyboardState);
  commandInput?.addEventListener("blur", () => window.setTimeout(updateMobileKeyboardState, 0));
  const commandReady = () => isConnected && initialOutputParsed
    && (host.dataset.paneModeCapable !== "true" || paneMode !== null)
    && paneMode?.dead !== true && paneMode?.in_mode !== true
    && socket?.readyState === WebSocket.OPEN;
  const updateCommandComposer = () => {
    if (!commandInput) return;
    const ready = commandReady();
    commandForm?.setAttribute("aria-busy", String(commandPending));
    const submitButton = commandForm?.querySelector('button[type="submit"]');
    if (submitButton) {
      submitButton.disabled = commandPending || !ready;
      submitButton.textContent = tr(commandPending ? "terminal.command_sending_button" : "terminal.run");
    }
    commandInput.style.height = "auto";
    commandInput.style.height = `${Math.min(commandInput.scrollHeight, 132)}px`;
    const lines = commandInput.value ? commandInput.value.split("\n").length : 0;
    if (commandMeta) {
      commandMeta.textContent =
        commandPending ? tr("terminal.command_sending")
        : commandUnconfirmed ? tr("terminal.command_unconfirmed")
        : paneMode?.dead ? tr("terminal.command_pane_ended", { pane: paneMode.pane })
        : paneMode?.in_mode ? tr("terminal.command_pane_in_mode", { pane: paneMode.pane })
        : !ready ? tr("terminal.command_waiting")
        : lines > 1
          ? tr("terminal.command_multiline", { lines })
          : tr("terminal.command_hint");
      if (ready && paneMode?.pane) {
        commandMeta.textContent = `${tr("terminal.command_target", { pane: paneMode.pane })} · ${commandMeta.textContent}`;
      }
    }
  };
  commandForm?.addEventListener("submit", (event) => {
    event.preventDefault();
    if (composerComposing || commandPending) return;
    if (!commandInput.value.trim()) return;
    if (!commandReady()) {
      setCommandDelivery(paneMode?.dead ? "pane_ended"
        : paneMode?.in_mode ? "pane_in_mode" : "not_sent");
      return;
    }
    const command = commandInput.value;
    const epoch = connectionEpoch;
    const commandSocket = socket;
    commandPending = true;
    setCommandDelivery("pending");
    pendingCommand = { id: String(++commandSequence), command, pane: paneMode?.pane };
    const submission = pendingCommand;
    commandUnconfirmed = true;
    persistCommandDraft();
    updateCommandComposer();
    // Parse already-received mode changes before negotiating native paste.
    // A switch/reconnect must not redirect this explicitly submitted command.
    pendingCommandPaste = () => {
      if (pendingCommand !== submission) return;
      if (
        epoch !== connectionEpoch || commandSocket !== socket ||
        socket?.readyState !== WebSocket.OPEN
      ) {
        resetCommandDelivery();
        return;
      }
      pastedCommand = command;
      pastedCommandSent = false;
      try {
        term.paste(command);
      } finally {
        pastedCommand = null;
      }
      if (!pastedCommandSent) resetCommandDelivery();
    };
    pastePendingCommand();
  });
  commandInput?.addEventListener("input", () => {
    commandUnconfirmed = pendingCommand?.command === commandInput.value;
    if (!commandPending) setCommandDelivery("idle");
    setInputError();
    persistCommandDraft();
    updateCommandComposer();
  });
  commandInput?.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      commandForm?.requestSubmit();
    }
  });
  restoreCommandDraft();

  const bindCommandReuse = (button) => {
    button.addEventListener("click", () => {
      setComposerOpen(true, { focus: false });
      commandInput.value = button.dataset.commandReuse || "";
      commandInput.dispatchEvent(new Event("input"));
      commandInput.focus();
      commandInput.setSelectionRange(commandInput.value.length, commandInput.value.length);
    });
  };
  document.querySelectorAll("[data-command-reuse]").forEach(bindCommandReuse);
  const rememberCommand = (command) => {
    const recent = document.querySelector("[data-recent-commands]");
    if (!recent) return;
    const previous = [...recent.querySelectorAll("[data-command-reuse]")]
      .find((button) => button.dataset.commandReuse === command);
    previous?.remove();
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.commandReuse = command;
    button.textContent = command;
    bindCommandReuse(button);
    recent.querySelector(".recent-command-label").after(button);
    [...recent.querySelectorAll("[data-command-reuse]")].slice(5).forEach((item) => item.remove());
    recent.hidden = false;
    document.querySelector("[data-command-empty]")?.setAttribute("hidden", "");
  };

  const terminalTabs = [...document.querySelectorAll("[data-terminal-switch]")];
  const terminalOutputLink = document.querySelector("[data-terminal-output-link]");
  const terminalManageForm = document.querySelector("[data-terminal-manage-form]");
  const terminalNameInput = document.querySelector("[data-terminal-name-input]");
  const terminalCommandClearTarget = document.querySelector(
    "[data-terminal-command-clear-target]",
  );
  const terminalHistoryState = (terminalId) => {
    const current = history.state && typeof history.state === "object" ? history.state : {};
    return { ...current, termroomTerminalId: terminalId };
  };
  const closeTerminalPopovers = () => {
    document.querySelectorAll(".terminal-chrome [data-popover]").forEach((popover) => {
      popover.removeAttribute("open");
      popover.querySelector("[data-popover-trigger]")?.setAttribute("aria-expanded", "false");
      popover.querySelectorAll("[data-popover-panel]").forEach((panel) => {
        panel.hidden = true;
      });
    });
  };
  const resetTerminalActivityState = () => {
    window.clearTimeout(activityAckTimer);
    pendingActivityAt = 0;
    acknowledgedActivityAt = 0;
    renderedActivityAt = 0;
    outputRenderSequence = 0;
    acknowledgedRenderSequence = 0;
  };
  const switchShellTerminal = (tab, { historyMode = "push" } = {}) => {
    const targetId = tab.dataset.terminalSwitch || "";
    const targetRole = tab.dataset.terminalRole || "shell";
    if (!targetId || !shellTerminal || targetRole !== "shell") return false;
    if (targetId === (pendingTerminalId || host.dataset.terminalId)) return true;
    const previousTerminalId = host.dataset.terminalId || "";

    window.dispatchEvent(new Event("termroom:terminal-switching"));
    persistCommandDraft();
    connectionEpoch += 1;
    resetInputControl();
    const epoch = connectionEpoch;
    pendingTerminalId = targetId;
    cancelPresenceRequest();
    window.clearTimeout(reconnectTimer);
    window.clearTimeout(otherInputTimer);
    const previousSocket = socket;
    socket = null;
    if (previousSocket && previousSocket.readyState < WebSocket.CLOSING) {
      previousSocket.close(1000, "terminal switched");
    }

    reconnectAllowed = true;
    reconnectDelay = 500;
    presenceInitialized = false;
    lastInputRevision = 0;
    resetTerminalActivityState();
    setComposerOpen(false, { focus: false });
    closeMoreKeys({ focus: false });
    closeTerminalPopovers();
    document.body.classList.remove("terminal-keyboard-open");

    setStatus(tr("terminal.status.connecting"));
    // reset() does not cancel xterm's asynchronous write queue. Drain old
    // writes under their old identity before reusing the terminal for a tab.
    term.write("", () => {
      if (epoch !== connectionEpoch) return;
      term.reset();
      term.clearSelection?.();
      host.dataset.terminalId = targetId;
      restoreCommandDraft();
      host.dataset.terminalRole = targetRole;
      shellTerminal = true;
      terminalTabs.forEach((candidate) => {
        const active = candidate === tab;
        candidate.classList.toggle("active", active);
        if (active) candidate.setAttribute("aria-current", "page");
        else candidate.removeAttribute("aria-current");
      });

      const workspaceId = host.dataset.workspaceId || "";
      terminalOutputLink?.setAttribute(
        "href",
        `/w/${encodeURIComponent(workspaceId)}/terminal/${encodeURIComponent(targetId)}/scrollback`,
      );
      terminalManageForm?.setAttribute(
        "action",
        `/w/${encodeURIComponent(workspaceId)}/terminals/${encodeURIComponent(targetId)}`,
      );
      if (terminalNameInput) {
        terminalNameInput.value = tab.dataset.terminalName || "shell";
      }
      if (terminalCommandClearTarget) terminalCommandClearTarget.value = targetId;
      host.setAttribute("aria-label", tr("terminal.screen", { name: tab.dataset.terminalName }));
      document.querySelector(".workspace-content")?.setAttribute("aria-label", tr("terminal.context", {
        project: host.dataset.workspaceName,
        path: host.dataset.workspacePath,
        computer: host.dataset.workspaceComputer,
        name: tab.dataset.terminalName,
      }));
      document.querySelector(".terminal-error-banner")?.remove();
      window.dispatchEvent(
        new CustomEvent("termroom:terminal-switched", {
          detail: {
            workspace_id: workspaceId,
            previous_terminal_id: previousTerminalId,
            terminal_id: targetId,
          },
        }),
      );
      if (historyMode === "push") {
        history.pushState(
          terminalHistoryState(targetId),
          "",
          tab.getAttribute("href") || location.href,
        );
      }
      pendingTerminalId = "";
      connect();
    });
    return true;
  };

  terminalTabs.forEach((tab) => {
    tab.addEventListener("click", (event) => {
      const modifiedClick = event.button !== 0
        || event.metaKey
        || event.ctrlKey
        || event.shiftKey
        || event.altKey;
      if (event.defaultPrevented || modifiedClick || tab.target === "_blank") return;
      if (switchShellTerminal(tab)) event.preventDefault();
    });
  });
  history.replaceState(
    terminalHistoryState(host.dataset.terminalId || ""),
    "",
    location.href,
  );
  window.addEventListener("popstate", (event) => {
    const stateTerminalId = event.state?.termroomTerminalId;
    const urlTerminalId = new URL(location.href).searchParams.get("terminal");
    const targetId = String(stateTerminalId || urlTerminalId || "");
    const tab = terminalTabs.find((candidate) => candidate.dataset.terminalSwitch === targetId);
    if (tab && switchShellTerminal(tab, { historyMode: "none" })) return;
    location.reload();
  });

  connect();
})();
