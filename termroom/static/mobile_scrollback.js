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

  const normalizeText = (value) =>
    String(value || "")
      .replace(/\r\n?/g, "\n")
      .replace(/\u00a0/g, " ");

  const historyOnlyUrl = () => {
    const url = new URL(outputLink.href, window.location.href);
    url.searchParams.set("history_only", "1");
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
    const hostStyle = window.getComputedStyle(terminalHost);
    const rowHeight = row.getBoundingClientRect().height;
    history.style.fontFamily = xtermStyle.fontFamily;
    history.style.fontSize = xtermStyle.fontSize;
    history.style.letterSpacing = xtermStyle.letterSpacing;
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
    if (away && mobileViewport.matches) {
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
      const historicalText = normalizeText(await response.text()).replace(/\n+$/, "");
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
      if (!stickToBottom && !liveFollowing && !atLiveBottom()) {
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
        renderedHistoryText = historicalText;
        history.textContent = historicalText;
        history.hidden = !historicalText;
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
      if (event.target === surface) noteUserScrollIntent({ revealHistory: true });
    },
    { capture: true, passive: true },
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
