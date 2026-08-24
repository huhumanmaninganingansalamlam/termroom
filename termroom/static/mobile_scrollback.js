(() => {
  const outputLink = document.querySelector('.terminal-output-action[href*="/scrollback"]');
  const terminalHost = document.querySelector("#terminal");
  if (!outputLink || !terminalHost) return;

  const mobileViewport = window.matchMedia("(max-width: 1023px), (pointer: coarse)");
  const SCROLL_BOTTOM_EPSILON_PX = 3;
  const TOUCH_DISTANCE_PX = 24;
  const TOUCH_DIRECTION_RATIO = 1.25;
  const TOUCH_HOLD_LIMIT_MS = 450;
  const HISTORY_REFRESH_DEBOUNCE_MS = 1200;
  const HISTORY_REFRESH_MAX_WAIT_MS = 3000;
  const HISTORY_BOUNDARY_ROW_COUNT = 6;
  const NATIVE_COPY_SELECTION_CLASS = "terminal-scroll-native-selection";
  const HISTORY_ANSI_MAX_CHARS = 2_000_000;
  const HISTORY_ANSI_MAX_SEGMENTS = 20_000;
  const ANSI_COLOR_VARIABLES = [
    "--terminal-black",
    "--terminal-red",
    "--terminal-green",
    "--terminal-yellow",
    "--terminal-blue",
    "--terminal-magenta",
    "--terminal-cyan",
    "--terminal-white",
    "--terminal-bright-black",
    "--terminal-bright-red",
    "--terminal-bright-green",
    "--terminal-bright-yellow",
    "--terminal-bright-blue",
    "--terminal-bright-magenta",
    "--terminal-bright-cyan",
    "--terminal-bright-white",
  ];
  const FULL_VIEWPORT_REDRAW_PREFIX = /^(?:\x1b\[[0-?]*[ -/]*[@-~])*\x1b\[H/;
  const CURSOR_SHOW_SEQUENCE = "\x1b[?25h";

  const surface = document.createElement("div");
  surface.className = "terminal-scroll-surface";
  surface.setAttribute("role", "group");

  const history = document.createElement("pre");
  history.className = "terminal-scroll-history";
  history.hidden = true;
  history.setAttribute("aria-label", outputLink.getAttribute("aria-label") || "Terminal history");

  const liveButton = document.createElement("button");
  liveButton.type = "button";
  liveButton.className = "terminal-scroll-live";
  liveButton.textContent = "LIVE ↓";
  liveButton.hidden = true;
  liveButton.setAttribute("aria-label", "Live terminal");

  terminalHost.before(surface);
  surface.append(history, terminalHost, liveButton);

  let loading = false;
  let refreshTimer = 0;
  let refreshQueued = false;
  let urgentRefreshQueued = false;
  let forceNextHistoryRefresh = false;
  let lastLiveHeight = 0;
  let touchGesture = null;
  let renderedHistoryText = "";
  let historyEtag = "";
  let historyChangeRevision = 0;
  let historyDirty = true;
  let historyDirtySince = performance.now();
  let lastHistoryChangeAt = historyDirtySince;
  let historyRenderRevision = 0;
  let terminalRevision = 0;
  let userScrollRevision = 0;
  let liveFollowing = true;
  let nativeCopySelectionActive = false;
  let nativeCopyPointerDown = false;

  const normalizeText = (value) =>
    String(value || "")
      .replace(/\r\n?/g, "\n")
      .replace(/\u00a0/g, " ");

  const stripAnsiHistoryControls = (value) =>
    normalizeText(value)
      .replace(/\x1b\][\s\S]*?(?:\x07|\x1b\\)/g, "")
      .replace(/\x1b[P^_X][\s\S]*?\x1b\\/g, "")
      .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "")
      .replace(/\x1b[()][0-2A-Z]/g, "")
      .replace(/\x1b[@-_]/g, "")
      .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "")
      .replace(/\n+$/, "");

  const createAnsiHistoryState = () => ({
    bold: false,
    dim: false,
    italic: false,
    underline: false,
    strike: false,
    overline: false,
    inverse: false,
    hidden: false,
    foreground: null,
    background: null,
  });

  const ansiHistoryColor = (color, brighten = false) => {
    if (!color) return "";
    if (color.kind === "rgb") {
      return `rgb(${color.red}, ${color.green}, ${color.blue})`;
    }
    let index = Number(color.index);
    if (!Number.isInteger(index) || index < 0 || index > 255) return "";
    if (brighten && index < 8) index += 8;
    if (index < ANSI_COLOR_VARIABLES.length) {
      return `var(${ANSI_COLOR_VARIABLES[index]})`;
    }
    if (index < 232) {
      const offset = index - 16;
      const levels = [0, 95, 135, 175, 215, 255];
      const red = levels[Math.floor(offset / 36) % 6];
      const green = levels[Math.floor(offset / 6) % 6];
      const blue = levels[offset % 6];
      return `rgb(${red}, ${green}, ${blue})`;
    }
    const level = 8 + (index - 232) * 10;
    return `rgb(${level}, ${level}, ${level})`;
  };

  const ansiHistoryStyle = (state) => {
    const style = {};
    const foreground = ansiHistoryColor(state.foreground, state.bold);
    const background = ansiHistoryColor(state.background);
    if (state.inverse) {
      style.color = background || "var(--terminal-bg)";
      style.backgroundColor = foreground || "var(--terminal-text)";
    } else {
      if (foreground) style.color = foreground;
      if (background) style.backgroundColor = background;
    }
    if (state.hidden) {
      style.color = style.backgroundColor || "var(--terminal-bg)";
    }
    if (state.bold) style.fontWeight = "700";
    if (state.dim) style.opacity = "0.72";
    if (state.italic) style.fontStyle = "italic";
    const decorations = [];
    if (state.underline) decorations.push("underline");
    if (state.strike) decorations.push("line-through");
    if (state.overline) decorations.push("overline");
    if (decorations.length) style.textDecorationLine = decorations.join(" ");
    return style;
  };

  const applyAnsiHistorySgr = (state, rawParameters) => {
    const values = rawParameters === ""
      ? [0]
      : rawParameters
          .split(/[;:]/)
          .filter((value) => value !== "")
          .map((value) => Number(value));
    const safeByte = (value) => Math.max(0, Math.min(255, Number(value) || 0));
    for (let index = 0; index < values.length; index += 1) {
      const code = Number.isFinite(values[index]) ? values[index] : 0;
      if (code === 0) Object.assign(state, createAnsiHistoryState());
      else if (code === 1) state.bold = true;
      else if (code === 2) state.dim = true;
      else if (code === 3) state.italic = true;
      else if (code === 4 || code === 21) state.underline = true;
      else if (code === 7) state.inverse = true;
      else if (code === 8) state.hidden = true;
      else if (code === 9) state.strike = true;
      else if (code === 22) {
        state.bold = false;
        state.dim = false;
      } else if (code === 23) state.italic = false;
      else if (code === 24) state.underline = false;
      else if (code === 27) state.inverse = false;
      else if (code === 28) state.hidden = false;
      else if (code === 29) state.strike = false;
      else if (code >= 30 && code <= 37) {
        state.foreground = { kind: "index", index: code - 30 };
      } else if (code === 39) state.foreground = null;
      else if (code >= 40 && code <= 47) {
        state.background = { kind: "index", index: code - 40 };
      } else if (code === 49) state.background = null;
      else if (code >= 90 && code <= 97) {
        state.foreground = { kind: "index", index: code - 90 + 8 };
      } else if (code >= 100 && code <= 107) {
        state.background = { kind: "index", index: code - 100 + 8 };
      } else if (code === 53) state.overline = true;
      else if (code === 55) state.overline = false;
      else if (code === 38 || code === 48 || code === 58) {
        const target = code === 38 ? "foreground" : code === 48 ? "background" : null;
        const mode = values[index + 1];
        if (mode === 5 && Number.isFinite(values[index + 2])) {
          if (target) {
            state[target] = { kind: "index", index: safeByte(values[index + 2]) };
          }
          index += 2;
        } else if (mode === 2) {
          let colorStart = index + 2;
          if (values.length - colorStart >= 4 && values[colorStart] === 0) {
            colorStart += 1;
          }
          if (values.length - colorStart >= 3) {
            if (target) {
              state[target] = {
                kind: "rgb",
                red: safeByte(values[colorStart]),
                green: safeByte(values[colorStart + 1]),
                blue: safeByte(values[colorStart + 2]),
              };
            }
            index = colorStart + 2;
          }
        }
      }
    }
  };

  const plainAnsiHistoryResult = (value) => {
    const text = stripAnsiHistoryControls(value);
    const fragment = document.createDocumentFragment();
    if (text) fragment.append(document.createTextNode(text));
    return { fragment, text, styled: false };
  };

  const parseAnsiHistory = (value) => {
    const source = normalizeText(value);
    if (source.length > HISTORY_ANSI_MAX_CHARS) return plainAnsiHistoryResult(source);
    const state = createAnsiHistoryState();
    const segments = [];
    let plainText = "";
    let offset = 0;
    let styled = false;

    const appendText = (rawText) => {
      const text = rawText.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "");
      if (!text) return;
      const style = ansiHistoryStyle(state);
      const key = JSON.stringify(style);
      const previous = segments[segments.length - 1];
      if (previous && previous.key === key) previous.text += text;
      else {
        if (segments.length >= HISTORY_ANSI_MAX_SEGMENTS) {
          throw new Error("ANSI history segment limit exceeded");
        }
        segments.push({ text, style, key });
      }
      if (Object.keys(style).length) styled = true;
      plainText += text;
    };

    const stringControlEnd = (start, allowBell) => {
      for (let index = start; index < source.length; index += 1) {
        if (allowBell && source.charCodeAt(index) === 7) return index + 1;
        if (source[index] === "\x1b" && source[index + 1] === "\\") return index + 2;
      }
      return source.length;
    };

    try {
      while (offset < source.length) {
        const escape = source.indexOf("\x1b", offset);
        if (escape < 0) {
          appendText(source.slice(offset));
          break;
        }
        if (escape > offset) appendText(source.slice(offset, escape));
        const introducer = source[escape + 1];
        if (introducer === "[") {
          let end = escape + 2;
          while (end < source.length) {
            const code = source.charCodeAt(end);
            if (code >= 0x40 && code <= 0x7e) break;
            end += 1;
          }
          if (end >= source.length) break;
          if (source[end] === "m") {
            applyAnsiHistorySgr(state, source.slice(escape + 2, end));
          }
          offset = end + 1;
        } else if (introducer === "]") {
          offset = stringControlEnd(escape + 2, true);
        } else if (["P", "X", "^", "_"].includes(introducer)) {
          offset = stringControlEnd(escape + 2, false);
        } else if (["(", ")", "*", "+", "-", ".", "/", "#"].includes(introducer)) {
          offset = Math.min(source.length, escape + 3);
        } else {
          offset = Math.min(source.length, escape + 2);
        }
      }
    } catch {
      return plainAnsiHistoryResult(source);
    }

    let removedTrailing = 0;
    while (segments.length) {
      const last = segments[segments.length - 1];
      const trimmed = last.text.replace(/\n+$/, "");
      removedTrailing += last.text.length - trimmed.length;
      if (trimmed) {
        last.text = trimmed;
        break;
      }
      segments.pop();
    }
    if (removedTrailing) plainText = plainText.slice(0, -removedTrailing);

    const fragment = document.createDocumentFragment();
    for (const segment of segments) {
      if (!Object.keys(segment.style).length) {
        fragment.append(document.createTextNode(segment.text));
        continue;
      }
      const span = document.createElement("span");
      Object.assign(span.style, segment.style);
      span.textContent = segment.text;
      fragment.append(span);
    }
    return { fragment, text: plainText, styled };
  };

  const enableNativeCopySelection = () => {
    nativeCopySelectionActive = true;
    nativeCopyPointerDown = true;
    surface.classList.add(NATIVE_COPY_SELECTION_CLASS);
  };

  const clearNativeCopySelection = ({ refreshHistory = true } = {}) => {
    const wasActive = nativeCopySelectionActive;
    nativeCopySelectionActive = false;
    nativeCopyPointerDown = false;
    surface.classList.remove(NATIVE_COPY_SELECTION_CLASS);
    if (refreshHistory && wasActive && historyDirty && liveFollowing) {
      scheduleHistoryRefresh({ urgent: true });
    }
  };

  const nativeCopySelectionStillStartsInHistory = () => {
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return false;
    return history.contains(selection.getRangeAt(0).startContainer);
  };

  const historyIsVisibleInSurface = () => {
    if (history.hidden) return false;
    const surfaceRect = surface.getBoundingClientRect();
    const historyRect = history.getBoundingClientRect();
    return (
      historyRect.bottom > surfaceRect.top + SCROLL_BOTTOM_EPSILON_PX
      && historyRect.top < surfaceRect.bottom - SCROLL_BOTTOM_EPSILON_PX
    );
  };

  const finishNativeCopyPointer = () => {
    if (!nativeCopySelectionActive) return;
    window.requestAnimationFrame(() => {
      nativeCopyPointerDown = false;
      if (!nativeCopySelectionStillStartsInHistory()) clearNativeCopySelection();
    });
  };

  const crossBoundaryCopyText = () => {
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return null;
    const range = selection.getRangeAt(0);
    if (
      !history.contains(range.startContainer)
      || !terminalHost.contains(range.endContainer)
    ) return null;

    const historyRange = document.createRange();
    historyRange.selectNodeContents(history);
    try {
      historyRange.setStart(range.startContainer, range.startOffset);
    } catch {
      return null;
    }
    const selectedHistory = historyRange.toString();
    const selectedText = selection.toString();
    if (!selectedHistory || !selectedText.startsWith(selectedHistory)) return null;

    // Chromium inserts one newline for the history block and another for the
    // first xterm row. The terminal stream has only one boundary newline.
    const liveTail = selectedText.slice(selectedHistory.length).replace(/^\n\n/, "\n");
    return selectedHistory + liveTail;
  };

  const historyOnlyUrl = () => {
    const url = new URL(outputLink.href, window.location.href);
    url.searchParams.set("history_only", "1");
    url.searchParams.set("ansi", "1");
    return url.href;
  };

  const maxScrollTop = () => Math.max(0, surface.scrollHeight - surface.clientHeight);

  const atLiveBottom = () =>
    surface.scrollTop >= maxScrollTop() - SCROLL_BOTTOM_EPSILON_PX;

  const afterLayout = (callback) => {
    if (document.hidden) {
      window.setTimeout(callback, 0);
    } else {
      window.requestAnimationFrame(callback);
    }
  };

  const historyBoundarySignature = (rows) => {
    const children = rows.children;
    const count = Math.min(
      HISTORY_BOUNDARY_ROW_COUNT,
      Math.max(0, children.length - 1),
    );
    const parts = [String(children.length)];
    for (let index = 0; index < count; index += 1) {
      parts.push(children[index].textContent || "");
    }
    return parts.join("\u0000");
  };

  const isFullViewportRedraw = (value) =>
    typeof value === "string" && FULL_VIEWPORT_REDRAW_PREFIX.test(value);

  const markHistoryDirty = () => {
    const now = performance.now();
    if (!historyDirty) historyDirtySince = now;
    historyDirty = true;
    lastHistoryChangeAt = now;
    historyChangeRevision += 1;
  };

  const clearHistoryDirty = () => {
    historyDirty = false;
    historyDirtySince = 0;
    lastHistoryChangeAt = 0;
  };

  const syncHistoryMetrics = () => {
    const xterm = terminalHost.querySelector(".xterm");
    const row = terminalHost.querySelector(".xterm-rows > div");
    if (!xterm || !row) return;
    const xtermStyle = window.getComputedStyle(xterm);
    const rowStyle = window.getComputedStyle(row);
    const hostStyle = window.getComputedStyle(terminalHost);
    const rowHeight = row.getBoundingClientRect().height;
    // The xterm container inherits the surrounding UI font size on some
    // desktop layouts, while the renderer rows use the actual terminal font
    // metrics. Follow the rendered row so history wraps at the same cell
    // width on every device instead of drifting by a pixel at the seam.
    history.style.fontFamily = rowStyle.fontFamily || xtermStyle.fontFamily;
    history.style.fontSize = rowStyle.fontSize || xtermStyle.fontSize;
    history.style.letterSpacing = rowStyle.letterSpacing || xtermStyle.letterSpacing;
    if (rowHeight > 0) history.style.lineHeight = `${rowHeight}px`;
    history.style.paddingLeft = hostStyle.paddingLeft;
    history.style.paddingRight = hostStyle.paddingRight;
  };

  const syncLiveHeight = () => {
    const height = Math.max(72, surface.clientHeight);
    if (Math.abs(height - lastLiveHeight) < 1) return;
    lastLiveHeight = height;
    terminalHost.style.height = `${height}px`;
    window.dispatchEvent(new Event("resize"));
  };

  const updateScrollState = () => {
    const wasFollowing = liveFollowing;
    const away = !atLiveBottom();
    liveFollowing = !away;
    liveButton.hidden = !away;
    document.body.classList.toggle("terminal-scroll-away", away);
    if (!away && !wasFollowing && historyDirty) {
      scheduleHistoryRefresh({ urgent: true });
    }
    // Once the user leaves live output, the terminal is in reading/copy mode.
    // Release xterm's hidden textarea on every pointer type so a drag that
    // begins in history is owned by the native document selection instead of
    // being redirected back into xterm. Clicking the live terminal focuses it
    // again through xterm's normal pointer handling.
    if (away) {
      document.querySelector(".xterm-helper-textarea")?.blur();
    }
  };

  const scrollToLive = ({ userInitiated = false } = {}) => {
    if (userInitiated) userScrollRevision += 1;
    const interactionRevision = userScrollRevision;
    liveFollowing = true;

    const align = () => {
      if (interactionRevision !== userScrollRevision || !liveFollowing) return;
      surface.scrollTop = maxScrollTop();
      updateScrollState();
    };

    align();
    afterLayout(align);
    if (userInitiated && historyDirty) {
      scheduleHistoryRefresh({ urgent: true });
    }
  };

  const loadHistory = async ({ stickToBottom = false, force = false } = {}) => {
    if (loading) {
      refreshQueued = true;
      urgentRefreshQueued ||= stickToBottom || force;
      forceNextHistoryRefresh ||= force;
      return;
    }
    loading = true;
    const requestedTerminalRevision = terminalRevision;
    const requestedChangeRevision = historyChangeRevision;
    surface.dataset.historyLoading = "true";
    try {
      const headers = { Accept: "text/plain" };
      if (historyEtag && !force) headers["If-None-Match"] = historyEtag;
      const response = await fetch(historyOnlyUrl(), {
        credentials: "same-origin",
        cache: "no-store",
        headers,
      });
      if (requestedTerminalRevision !== terminalRevision) {
        refreshQueued = true;
        urgentRefreshQueued = true;
        forceNextHistoryRefresh = true;
        return;
      }
      if (response.status === 304) {
        if (historyChangeRevision === requestedChangeRevision) {
          clearHistoryDirty();
        } else {
          refreshQueued = true;
          forceNextHistoryRefresh = true;
        }
        return;
      }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const responseEtag = response.headers.get("etag") || historyEtag;
      const historicalText = normalizeText(await response.text());
      if (requestedTerminalRevision !== terminalRevision) {
        refreshQueued = true;
        urgentRefreshQueued = true;
        forceNextHistoryRefresh = true;
        return;
      }

      // History is a stable reading snapshot. If the user left live while this
      // request was in flight, keep the existing DOM untouched. A bounded tmux
      // capture can grow or shrink as old rows roll out; replacing it while the
      // user reads can otherwise clamp scrollTop even when coordinates are
      // restored exactly. Refresh once the user returns to live instead.
      if (
        nativeCopySelectionActive
        || (!stickToBottom && !liveFollowing && !atLiveBottom())
      ) {
        if (!historyDirty) {
          historyDirtySince = performance.now();
          lastHistoryChangeAt = historyDirtySince;
        }
        historyDirty = true;
        return;
      }

      // A live-following capture remains useful even if more output arrived
      // while it was in flight. Render that bounded server snapshot, keep the
      // dirty bit, and let max-wait scheduling reconcile the next one. If the
      // user left live, the stable-reading guard above discards it instead.
      const captureBecameStale = historyChangeRevision !== requestedChangeRevision;
      if (captureBecameStale) {
        historyDirty = true;
        historyDirtySince = performance.now();
        lastHistoryChangeAt = historyDirtySince;
        forceNextHistoryRefresh = true;
      } else {
        clearHistoryDirty();
      }
      // The validator represents the text currently committed to the history
      // DOM. A response discarded while the user reads must not advance it,
      // otherwise LIVE could receive 304 while still showing an older capture.
      historyEtag = responseEtag;

      const keepBottom = stickToBottom || liveFollowing || atLiveBottom();
      const previousTop = surface.scrollTop;
      const interactionRevision = userScrollRevision;
      const historyChanged = historicalText !== renderedHistoryText;

      if (historyChanged) {
        const rendered = parseAnsiHistory(historicalText);
        renderedHistoryText = historicalText;
        history.replaceChildren(rendered.fragment);
        history.hidden = !rendered.text;
        history.dataset.rendering = rendered.styled ? "ansi" : "plain";
        syncHistoryMetrics();
      }

      const renderRevision = ++historyRenderRevision;
      if (historyChanged || keepBottom) {
        afterLayout(() => {
          if (
            renderRevision !== historyRenderRevision
            || interactionRevision !== userScrollRevision
          ) return;
          syncLiveHeight();
          if (keepBottom && liveFollowing) {
            surface.scrollTop = maxScrollTop();
          } else if (!liveFollowing) {
            surface.scrollTop = Math.min(previousTop, maxScrollTop());
          }
          updateScrollState();
        });
      }
    } catch {
      // Keep the live terminal usable even if historical capture is unavailable.
    } finally {
      loading = false;
      delete surface.dataset.historyLoading;
      if (refreshQueued) {
        refreshQueued = false;
        const urgent = urgentRefreshQueued || !liveFollowing;
        urgentRefreshQueued = false;
        scheduleHistoryRefresh({ urgent });
      }
    }
  };

  const scheduleHistoryRefresh = ({ urgent = false, force = false } = {}) => {
    forceNextHistoryRefresh ||= force;
    if (!historyDirty || !liveFollowing) return;
    window.clearTimeout(refreshTimer);
    if (loading) {
      refreshQueued = true;
      return;
    }
    const now = performance.now();
    const dueAt = urgent
      ? now
      : Math.min(
          lastHistoryChangeAt + HISTORY_REFRESH_DEBOUNCE_MS,
          historyDirtySince + HISTORY_REFRESH_MAX_WAIT_MS,
        );
    refreshTimer = window.setTimeout(() => {
      refreshTimer = 0;
      if (!liveFollowing || document.hidden || mouseTrackingActive()) return;
      const useForce = forceNextHistoryRefresh;
      forceNextHistoryRefresh = false;
      void loadHistory({ force: useForce });
    }, Math.max(0, dueAt - now));
  };

  const bindLiveOutputRefresh = () => {
    const rows = terminalHost.querySelector(".xterm-rows");
    if (!rows) {
      window.setTimeout(bindLiveOutputRefresh, 80);
      return;
    }
    syncHistoryMetrics();
    let boundarySignature = historyBoundarySignature(rows);
    new MutationObserver(() => {
      const nextSignature = historyBoundarySignature(rows);
      if (nextSignature === boundarySignature) return;
      boundarySignature = nextSignature;
      markHistoryDirty();
      if (document.hidden || mouseTrackingActive() || !liveFollowing) return;
      scheduleHistoryRefresh();
    }).observe(rows, {
      childList: true,
      subtree: true,
      characterData: true,
    });
  };

  const bindParsedTerminalOutputRefresh = () => {
    const terminalPrototype = window.Terminal?.prototype;
    if (!terminalPrototype || typeof terminalPrototype.write !== "function") return;

    const nativeWrite = terminalPrototype.write;
    const redrawState = new WeakMap();
    terminalPrototype.write = function termroomScrollbackWrite(value, callback) {
      const text = typeof value === "string" ? value : "";
      const state = redrawState.get(this) || { fullViewport: false };
      if (isFullViewportRedraw(text)) state.fullViewport = true;
      const suppressHistoryRefresh = state.fullViewport;
      if (state.fullViewport && text.includes(CURSOR_SHOW_SEQUENCE)) {
        state.fullViewport = false;
      }
      redrawState.set(this, state);
      return nativeWrite.call(this, value, () => {
        if (
          this.element
          && terminalHost.contains(this.element)
          && !suppressHistoryRefresh
        ) {
          markHistoryDirty();
          if (!document.hidden && liveFollowing && !mouseTrackingActive()) {
            scheduleHistoryRefresh();
          }
        }
        if (typeof callback === "function") callback();
      });
    };
  };

  const mouseTrackingActive = () =>
    Boolean(terminalHost.querySelector(".xterm.enable-mouse-events"));

  const noteUserScrollIntent = ({ revealHistory = false } = {}) => {
    userScrollRevision += 1;
    if (revealHistory && historyDirty && liveFollowing) {
      scheduleHistoryRefresh({ urgent: true });
    }
  };

  surface.addEventListener("scroll", updateScrollState, { passive: true });
  liveButton.addEventListener("click", () => {
    scrollToLive({ userInitiated: true });
  });

  surface.addEventListener(
    "wheel",
    (event) => {
      if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
      const target = event.target instanceof Node ? event.target : null;
      if (target && terminalHost.contains(target) && mouseTrackingActive()) return;
      noteUserScrollIntent({ revealHistory: event.deltaY < 0 });
    },
    { capture: true, passive: true },
  );

  surface.addEventListener(
    "pointerdown",
    (event) => {
      const target = event.target instanceof Node ? event.target : null;
      const startsInHistory = Boolean(target && history.contains(target) && !history.hidden);
      const startsInTerminal = Boolean(target && terminalHost.contains(target));
      const useNativeCopySelection =
        event.button === 0
        && event.pointerType !== "touch"
        && !mouseTrackingActive()
        && (startsInHistory || (startsInTerminal && historyIsVisibleInSurface()));

      if (useNativeCopySelection) {
        enableNativeCopySelection();
        if (startsInTerminal) {
          terminalHost.querySelector(".xterm-helper-textarea")?.blur();
          event.stopPropagation();
        }
      } else if (nativeCopySelectionActive) {
        clearNativeCopySelection();
      }
      if (target === surface) noteUserScrollIntent({ revealHistory: true });
    },
    { capture: true, passive: true },
  );

  // xterm starts its cell-aware selection on mousedown. When history is visible
  // and native cross-boundary selection was chosen on pointerdown, keep the
  // browser's default text-selection action but do not let xterm replace it.
  terminalHost.addEventListener(
    "mousedown",
    (event) => {
      if (
        !nativeCopySelectionActive
        || event.button !== 0
        || mouseTrackingActive()
      ) return;
      event.stopImmediatePropagation();
    },
    { capture: true, passive: true },
  );

  document.addEventListener("pointerup", finishNativeCopyPointer, {
    capture: true,
    passive: true,
  });
  document.addEventListener("pointercancel", finishNativeCopyPointer, {
    capture: true,
    passive: true,
  });
  document.addEventListener("selectionchange", () => {
    if (
      nativeCopySelectionActive
      && !nativeCopyPointerDown
      && !nativeCopySelectionStillStartsInHistory()
    ) {
      clearNativeCopySelection();
    }
  });
  document.addEventListener(
    "copy",
    (event) => {
      if (!nativeCopySelectionActive || !event.clipboardData) return;
      const copyText = crossBoundaryCopyText();
      if (copyText === null) return;
      event.clipboardData.setData("text/plain", copyText);
      event.preventDefault();
    },
    { capture: true },
  );

  const bindLiveInputReturn = () => {
    const textarea = terminalHost.querySelector(".xterm-helper-textarea");
    if (!textarea) {
      window.setTimeout(bindLiveInputReturn, 80);
      return;
    }
    const returnToLive = () => {
      if (!atLiveBottom()) scrollToLive({ userInitiated: true });
    };
    textarea.addEventListener(
      "keydown",
      (event) => {
        if (["Shift", "Control", "Alt", "Meta", "CapsLock"].includes(event.key)) return;
        returnToLive();
      },
      { capture: true },
    );
    textarea.addEventListener("compositionstart", returnToLive, { capture: true });
  };

  document.querySelector("#command-form")?.addEventListener(
    "submit",
    () => {
      if (!atLiveBottom()) scrollToLive({ userInitiated: true });
    },
    { capture: true },
  );
  document.querySelector(".terminal-composer")?.addEventListener(
    "click",
    (event) => {
      const target = event.target instanceof Element ? event.target : null;
      if (
        target?.closest(
          '[data-terminal-key], [data-terminal-action], #paste-terminal, #focus-terminal',
        )
        && !atLiveBottom()
      ) {
        scrollToLive({ userInitiated: true });
      }
    },
    { capture: true },
  );

  // xterm consumes wheel events even when the attached tmux view has no xterm
  // scrollback. In the normal shell case, stop the event before xterm handles
  // it and let the browser's default action scroll the outer native surface.
  // Mouse-reporting TUIs keep ownership of the wheel unchanged.
  terminalHost.addEventListener(
    "wheel",
    (event) => {
      if (
        mouseTrackingActive()
        || event.ctrlKey
        || event.metaKey
        || event.altKey
        || event.shiftKey
      ) return;
      event.stopPropagation();
    },
    { capture: true, passive: true },
  );

  // On touch devices, preserve long-press selection and horizontal gestures.
  // Once a quick vertical drag is clearly intended as scrolling, stop xterm's
  // handlers from consuming it while leaving the browser's native pan action
  // untouched. At the live bottom only a downward finger drag can reveal the
  // history above; once already in history both directions remain native.
  terminalHost.addEventListener(
    "touchstart",
    (event) => {
      if (
        !mobileViewport.matches
        || event.touches.length !== 1
        || mouseTrackingActive()
      ) return;
      const touch = event.touches[0];
      touchGesture = {
        id: touch.identifier,
        x: touch.clientX,
        y: touch.clientY,
        startedAt: performance.now(),
        startedAtBottom: atLiveBottom(),
        scrolling: false,
      };
    },
    { capture: true, passive: true },
  );

  terminalHost.addEventListener(
    "touchmove",
    (event) => {
      if (!touchGesture) return;
      const touch = [...event.touches].find((item) => item.identifier === touchGesture.id);
      if (!touch) return;
      const dx = touch.clientX - touchGesture.x;
      const dy = touch.clientY - touchGesture.y;

      if (!touchGesture.scrolling) {
        if (performance.now() - touchGesture.startedAt > TOUCH_HOLD_LIMIT_MS) {
          touchGesture = null;
          return;
        }
        if (Math.abs(dx) < TOUCH_DISTANCE_PX && Math.abs(dy) < TOUCH_DISTANCE_PX) return;
        if (
          Math.abs(dy) < Math.abs(dx) * TOUCH_DIRECTION_RATIO
          || (touchGesture.startedAtBottom && dy < TOUCH_DISTANCE_PX)
          || mouseTrackingActive()
        ) {
          touchGesture = null;
          return;
        }
        touchGesture.scrolling = true;
        noteUserScrollIntent({ revealHistory: dy > 0 });
      }

      event.stopPropagation();
    },
    { capture: true, passive: true },
  );

  const clearTouchGesture = () => {
    touchGesture = null;
  };
  terminalHost.addEventListener("touchend", clearTouchGesture, {
    capture: true,
    passive: true,
  });
  terminalHost.addEventListener("touchcancel", clearTouchGesture, {
    capture: true,
    passive: true,
  });

  const resizeObserver = new ResizeObserver(() => {
    const shouldFollow = liveFollowing;
    syncLiveHeight();
    syncHistoryMetrics();
    if (shouldFollow) scrollToLive();
  });
  resizeObserver.observe(surface);

  window.addEventListener("focus", () => scheduleHistoryRefresh({ urgent: true }));
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) scheduleHistoryRefresh({ urgent: true });
  });
  window.addEventListener("termroom:terminal-switched", () => {
    clearNativeCopySelection({ refreshHistory: false });
    terminalRevision += 1;
    historyRenderRevision += 1;
    userScrollRevision += 1;
    window.clearTimeout(refreshTimer);
    refreshTimer = 0;
    refreshQueued = loading;
    urgentRefreshQueued = loading;
    forceNextHistoryRefresh = true;
    renderedHistoryText = "";
    historyEtag = "";
    history.textContent = "";
    history.hidden = true;
    touchGesture = null;
    liveFollowing = true;
    liveButton.hidden = true;
    document.body.classList.remove("terminal-scroll-away");
    markHistoryDirty();
    if (loading) return;
    void loadHistory({ stickToBottom: true, force: true });
  });

  syncLiveHeight();
  bindLiveInputReturn();
  bindLiveOutputRefresh();
  bindParsedTerminalOutputRefresh();
  void loadHistory({ stickToBottom: true, force: true });
  afterLayout(() => {
    syncLiveHeight();
    syncHistoryMetrics();
    if (atLiveBottom()) scrollToLive();
  });
})();
