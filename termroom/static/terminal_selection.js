(function installTerminalSelectionOwnership(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.TermroomSelectionOwnership = api;
})(typeof window === "undefined" ? globalThis : window, () => {
  "use strict";

  const LIVE_XTERM = "live-xterm";
  const READING_NATIVE = "reading-native";
  const TUI_MOUSE = "tui-mouse";

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
    if (event.type !== "reset" && event.mouseTracking) return TUI_MOUSE;
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
      case "reset":
        return LIVE_XTERM;
      default:
        throw new Error(`Unknown terminal selection event: ${event.type}`);
    }
  };

  return Object.freeze({
    LIVE_XTERM,
    READING_NATIVE,
    TUI_MOUSE,
    createMouseTrackingEdge,
    transition,
  });
});
