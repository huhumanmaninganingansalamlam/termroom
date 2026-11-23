(function installTerminalSelectionOwnership(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.TermroomSelectionOwnership = api;
})(typeof window === "undefined" ? globalThis : window, () => {
  "use strict";

  const LIVE_XTERM = "live-xterm";
  const READING_NATIVE = "reading-native";
  const TUI_MOUSE = "tui-mouse";
  const COPY_XTERM = "copy-xterm";

  const createMouseTrackingEdge = (onChange) => {
    let previous;
    return (active) => {
      const current = Boolean(active);
      if (current === previous) return false;
      previous = current;
      onChange(current);
      return true;
    };
  };

  const transition = (mode, event) => {
    if (event.type === "reset") return LIVE_XTERM;
    if (event.type === "start-copy") return COPY_XTERM;
    if (event.type === "stop-copy") {
      return event.mouseTracking ? TUI_MOUSE : event.away ? READING_NATIVE : LIVE_XTERM;
    }
    // Copy mode is explicit: incoming TUI mouse modes, output and focus
    // transitions cannot silently resume PTY mouse/input reporting.
    if (mode === COPY_XTERM) return COPY_XTERM;
    if (event.mouseTracking) return TUI_MOUSE;
    switch (event.type) {
      case "mouse-tracking":
        if (event.active) return TUI_MOUSE;
        return event.away ? READING_NATIVE : LIVE_XTERM;
      case "enter-reading":
        return event.mouseTracking ? TUI_MOUSE : READING_NATIVE;
      case "return-live":
        return event.hasSurfaceSelection && mode === READING_NATIVE
          ? READING_NATIVE
          : LIVE_XTERM;
      case "surface-pointer":
        if (event.mouseTracking) return TUI_MOUSE;
        if (!event.primaryMouse) return mode;
        return event.away ? READING_NATIVE : LIVE_XTERM;
      case "pointer-finished":
      case "wheel":
        return mode;
      case "terminal-input":
      case "outside-pointer":
        return LIVE_XTERM;
      default:
        throw new Error(`Unknown terminal selection event: ${event.type}`);
    }
  };

  // xterm 6 disables cell selection whenever its mouse protocol is enabled.
  // Preserve the TUI-requested protocol (including changes while copying),
  // expose only the reversible toggle, and let xterm restore its own selection
  // service through its existing onProtocolChange path. The sole private xterm
  // boundary stays here; no private service is exposed on the DOM host.
  const createCopyMouseGate = (mouseService) => {
    const protocol = Object.getOwnPropertyDescriptor(
      Object.getPrototypeOf(mouseService), "activeProtocol",
    );
    if (!protocol?.get || !protocol?.set) return null;
    let requestedProtocol = mouseService.activeProtocol;
    let selecting = false;
    Object.defineProperty(mouseService, "activeProtocol", {
      configurable: true,
      get() { return protocol.get.call(mouseService); },
      set(value) {
        requestedProtocol = value;
        protocol.set.call(mouseService, selecting ? "NONE" : value);
      },
    });
    return (active) => {
      selecting = Boolean(active);
      protocol.set.call(mouseService, selecting ? "NONE" : requestedProtocol);
    };
  };

  // The copy-visible action intentionally excludes older scrollback and joins
  // terminal soft-wrapped rows, but keeps real line boundaries and Unicode.
  const visibleTerminalText = (terminal) => {
    const buffer = terminal.buffer.active;
    const lines = [];
    for (let row = 0; row < terminal.rows; row++) {
      const line = buffer.getLine(buffer.viewportY + row);
      if (!line) continue;
      const text = line.translateToString(true).replace(/\u00a0/g, " ");
      if (line.isWrapped && lines.length) lines[lines.length - 1] += text;
      else lines.push(text);
    }
    while (lines.length && !lines[lines.length - 1]) lines.pop();
    return lines.join("\n");
  };

  return Object.freeze({
    LIVE_XTERM,
    READING_NATIVE,
    TUI_MOUSE,
    COPY_XTERM,
    createMouseTrackingEdge,
    createCopyMouseGate,
    visibleTerminalText,
    transition,
  });
});
