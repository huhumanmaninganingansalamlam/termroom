# Changelog

All notable changes to Termroom are documented in this file.

## [0.4.18] - 2026-10-25

### Fixed

- Include the currently rendered terminal rows in browser accessibility
  snapshots when spoken screen-reader output is off. Current TUI output stays
  visible alongside scrollback instead of exposing only older history. Use
  xterm's native accessibility rows when screen-reader output is enabled to
  avoid duplicate screen content.

## [0.4.17] - 2026-10-24

### Fixed

- Allow tmux mouse clicks to select a living neighbour when the selected shell
  pane has ended. Keep command submission to the ended pane blocked and retain
  unsent drafts for explicit retry after selecting a live pane. Residual mouse
  protection for living shells remains enabled.

## [0.4.16] - 2026-10-24

### Fixed

- Keep command-editor drafts unsent while the selected pane is in tmux copy
  or clock mode. Show why Run is unavailable and preserve the draft across
  reloads; leaving the mode never automatically sends the draft.
- Reject SSH and Local command submissions if the pane enters a tmux mode
  before the server writes input. Return an explicit not-sent acknowledgement
  without adding command history or consuming the draft. Native mode keys
  remain available; Termroom does not cancel the mode on the user's behalf.

## [0.4.15] - 2026-10-24

### Fixed

- Keep command-editor drafts and show the selected split pane when that pane
  has ended. A connected tmux client is not proof that the selected pane can
  receive input; disable Run for a known ended pane without closing the editor.
- Bind SSH and Local command submissions to the displayed pane and check its
  current state before writing to the PTY. Reject ended, changed or unavailable
  targets without a success acknowledgement or recent-command entry. Keep the
  draft for explicit retry; never automatically resend after pane selection.

## [0.4.14] - 2026-10-24

### Fixed

- Restore the terminal's reading position after switching terminals or returning
  from another page in the same browser tab. Keep LIVE and new user input in
  control of returning to the latest screen; store coordinates, not output logs.

### Added

- Read and copy the changed portion of terminal output from the existing Output
  view, comparing with that page's previous capture. Keep full history available,
  distinguish unchanged captures and removed lines, and preserve the last capture
  and comparison baseline when a refresh fails.

## [0.4.13] - 2026-10-23

### Fixed

- Keep Local and Node PTY output moving while a tmux pane-state query is slow.
  Share the existing bounded refresh behavior with SSH, with one outstanding
  query and eventual state recovery even when no further output arrives.
- Wait for idle Node PTYs through the event loop instead of occupying the
  shared executor needed by input and other requests.
- Show the initial screen as pending, with a reconnect action, until both
  current pane state and parsed PTY output are available. Preserve unsent
  drafts and do not equate connection or PTY acknowledgement with app progress.

### Changed

- Add content-free connection, first-output, pane-availability and command-ACK
  diagnostics to normal server logs, with current-connection received/parsed
  frame counters in the terminal DOM. No terminal text or credentials are logged.

## [0.4.12] - 2026-10-23

### Fixed

- Keep command execution disabled until the current terminal backend is ready
  and its initial PTY screen has been parsed. Show that the draft is unsent;
  becoming ready does not automatically submit it. Reconnect retains drafts
  and uncertain-delivery state without replaying input.

### Changed

- Label a successful PTY acknowledgement as terminal receipt, not application
  acceptance. Label the home output indicator as terminal activity: pane
  redraws can advance it even when an application has no new response.

## [0.4.11] - 2026-10-15

### Fixed

- Preserve TUI output when opening the command editor before the first SSH
  screen arrives. Keep the empty xterm alternate buffer at the actual viewport
  capacity instead of allowing hidden scrollback to displace later redraws.
- Mark browser network loss immediately, retain unsent or unconfirmed drafts,
  and reconnect the same terminal when connectivity returns without replaying
  commands or treating WebSocket open as backend readiness.
- Make acknowledged commands immediately recoverable as unsent drafts in Recent,
  including on desktop, without requiring a page reload. A terminal delivery
  acknowledgement remains distinct from the application's actual response.

## [0.4.10] - 2026-10-13

### Fixed

- Keep connection state intact when clipboard access fails, and show the paste
  error separately. Expose reconnect or sign-in actions for the relevant failure
  without replaying unconfirmed input.
- Show command submission as pending and prevent duplicate clicks; a new unsent
  draft no longer inherits the previous command's delivery acknowledgement.
- Force UTF-8 for browser tmux clients so Korean and other Unicode output remains
  readable even when the computer's login locale is not UTF-8. Shell and Codex
  options and environments are unchanged.

### Changed

- Give native history keys clear previous, next, first and latest screen labels.
  They navigate the application's actual terminal view, not a separate transcript.

## [0.4.9] - 2026-10-13

### Fixed

- Prevent residual TUI mouse modes from inserting numeric mouse reports into
  shell commands after an application exits or a terminal view changes. Use
  the current canonical foreground shell state across Local, SSH and Node;
  native TUI/copy-mode mouse, keyboard, paste and drafts remain intact.
- Align contribution and terminal validation guidance with affected-flow
  runtime checks instead of obsolete mandatory Mac/Safari release gates.

## [0.4.8] - 2026-10-12

### Fixed

- Keep the terminal connection indicator pending until the current backend
  confirms its tmux pane. A WebSocket opening, presence count or arbitrary
  output no longer reports a failed or still-preparing SSH terminal as connected.
- Preserve the existing reconnect/failure indicators, verified recovery and
  unsent command drafts; stale connections cannot mark a new terminal ready.

This corrects the connection indicator, not the separate bin host/network outage.
Terminal delivery acknowledgements and application responses remain separate.

## [0.4.7] - 2026-10-12

### Changed

- Give projects stable accessible names including their computer and path, and
  keep the current workspace/terminal context accurate after terminal switching.
- Name the command editor and retain a separate delivery acknowledgement after
  it closes. Terminal delivery does not imply application admission or response;
  unconfirmed/rejected input keeps its draft and is never automatically replayed.
- Label xterm's opt-in displayed-row accessibility tree for small, current-screen
  snapshots without collecting another transcript or changing default modes.
- Add native PageUp, PageDown, Ctrl+Home and Ctrl+End keys to the existing helper
  menu for application-owned history navigation, including Codex's past/latest view.

Codex options and native mouse/paste handling are unchanged. An application which
does not request mouse capture is not claimed to support wheel navigation.

## [0.4.6] - 2026-10-12

### Fixed

- Wait for the current connection's initial PTY output to be parsed before
  command-editor paste, so native paste modes are ready even when the WebSocket
  opens before terminal initialization completes.
- Preserve admitted command-editor delivery across page navigation and retain
  unsent or unacknowledged drafts without automatically replaying them.
- Record recent command execution only after the terminal acknowledges input.
- Align the Python module version with the distribution version.

The startup-mode race and navigation/acknowledgement path are independently
verified. The original 10:02 command failure lacks a transport trace; separate
intermittent rendering stalls and historical CPU100/504 causes are not attributed
to these fixes. Codex defaults and running sessions are unchanged.

## [0.4.5] - 2026-10-11

### Changed

- Automatically transfer exclusive terminal input control on user activation;
  other browser connections remain view-only. Passive reconnects and viewport
  changes do not take control.
- Serialize terminal input and control transfer so an admitted paste finishes
  before another connection takes control, across Local, SSH and Node terminals.

### Fixed

- Wait for PTY writability after nonblocking backpressure (`EAGAIN`) and retry
  interrupted writes, preserving long-paste tails instead of closing the browser
  connection after a partial write on the actual uvloop server path.
- Preserve admitted terminal input when a competing viewport invalidates a
  resize result instead of silently dropping the complete input frame.
- Wait for queued xterm output to apply paste-mode changes before submitting
  command-editor text; preserve connection identity and concurrent draft edits.

Both nonblocking long-write interruption and the ownership race are reproduced
and corrected independently. Exact traces for the original production paste and
historical CPU100/504 causes remain unconfirmed.

## [0.4.4] - 2026-10-11

### Fixed

- Complete partial PTY input writes on Local, SSH and Node terminals, preserving
  long-paste tails, bracketed-paste terminators and subsequent input order.
- Retain command-editor drafts when the browser cannot enqueue the command;
  do not automatically replay commands after reconnecting.
- Keep SSH pane queries and queued database bookkeeping from delaying already
  received terminal output; retain the latest coalesced output timestamp.
- Keep Local tmux page queries, terminal actions and file I/O off the Core event
  loop, while preserving conflicting editor drafts during concurrent saves.
- Preserve alternate-screen content when a hidden terminal viewport has zero size.

Native paste negotiation and Codex defaults are unchanged. The command editor
first used native paste in 0.4.3; an older single-write defect could discard its
terminator and leave following input inside unfinished paste parsing. The complete
write path is verified with isolated Local/SSH/Node PTYs and an actual offline
Codex draft. Exact production byte-loss traces and the causes of historical
CPU100/504 incidents remain unconfirmed.

## [0.4.3] - 2026-10-07

### Fixed

- Preserve native bracketed paste for command-editor input on Local, SSH and Node terminals.
- Let actual command-editor input claim the shared terminal grid across browser views.
- Retire cached page connection errors only after verified recovery of the current terminal.
- Keep idle SSH terminal reads from occupying the shared executor used by pane queries and database operations.

These fixes do not establish the cause of the earlier production CPU100/504 incidents
or confirm resolution of the reported partial-output stall on 0.4.2.

## [0.4.0] - 2026-10-02

### Changed

- Require Core and Termroom Node to use the same Node protocol v3 generation; update both together.
- Keep Remote Run output and result recovery available across reconnects, and preserve the boundary between terminal history and live output.
- Give the first active terminal viewport one-shot ownership of the shared tmux grid, and apply input only after a successful resize.
- Keep file and run operations within their validated local, remote, and managed-root boundaries. Recheck source conflicts before applying results, never propagate remote deletions to the source, and delete managed data only when its exact ownership identity matches.
- Keep Local terminal setup, PTY writes, and cleanup from blocking the Core event loop.

### Compatibility and validation

- Certificate-dependent Safari validation is excluded from this release and is not a release claim. No TLS or trust bypass is introduced.
- These changes do not establish the cause of the earlier production incident.

## [0.3.18] - 2026-08-30

### Added

- Offer an opt-in terminal screen-reader mode and expose fresh connection state for active File Run and Remote Run sessions.

### Fixed

- Stabilize SSH history-only scrollback capture and tmux history readiness before rendering terminal output.
- Keep terminal text selections active while releasing the pointer or scrolling through history, without letting terminal mouse-tracking mode or unrelated DOM changes clear the selected range.

## [0.3.17] - 2026-08-24

### Fixed

- Size a newly recreated tmux grid from its first browser viewport without allowing passive devices to take over an established terminal.
- Recover a missing canonical Workspace tmux session from surviving browser views while preserving linked windows, active panes, and scrollback across Local, SSH, and Node backends.
- Render composed, decomposed, compatibility, halfwidth, extended, and tone-mark Hangul in historical output using xterm's active Unicode cell widths while preserving ANSI styling, wrapping, selection, and copied text.

## [0.3.16] - 2026-08-24

### Added

- Preserve tmux history colors, backgrounds, and text attributes across Local, SSH, and Node terminals with a safe plain-text fallback.

### Fixed

- Continue large native text selections across the history and live xterm boundary in either direction, including automatic scrolling and clean copied line breaks.
- Reflow styled history for each browser width while matching the live xterm font size and line height.
- Keep Workspace commands compact with the original click-to-edit cards, reveal additional slots through an explicit add button, and save, cancel, or delete each card independently.
- Keep a usable shell alive in the same tmux window after each Workspace command finishes, with a compact success or exit-status marker instead of tmux's raw dead-pane banner.

## [0.3.15] - 2026-08-24

### Added

- Show the canonical Workspace tmux session name beside the terminal tabs, with an external attach command in its tooltip.

### Changed

- Name newly registered persistent Workspaces with the short, readable `tr-<workspace>-<4-char ID>` pattern while keeping existing stored session names unchanged.
- Leave tmux status-line configuration entirely under the user's control.

## [0.3.14] - 2026-08-23

### Changed

- Switch between existing shell terminals inside a Workspace without reloading the full page, while keeping the selected URL, browser back/forward history, output link, and terminal management controls aligned.
- Keep managed Run terminals on their existing server-navigation path and continue discovering tmux windows created outside the UI on an explicit page refresh instead of background polling.

### Fixed

- Reset native terminal scrollback state and reject stale WebSocket, presence, and history responses when changing shell terminals in place.

## [0.3.13] - 2026-08-23

### Added

- Support Linux and macOS SSH computers through the same Workspace flow, including Homebrew-installed `tmux` and `git` on Apple Silicon and Intel Macs.

### Changed

- Traverse recent SSH files through bounded SFTP operations instead of GNU-only `timeout` and `find` flags.

### Fixed

- Resolve remote commands through the authenticated user's configured login shell, so `command -v tmux` sees the same properly installed command without hard-coded Homebrew or package-manager directories.

## [0.3.12] - 2026-08-22

### Added

- Search the current folder recursively across Local, SSH, and Node Workspaces with bounded traversal, path-aware results, and safe fallback behavior.

### Changed

- Compress eligible dynamic HTML, JSON, and text responses with gzip and apply nonce-based CSP, a minimal Permissions Policy, and proxy-safe security headers.
- Keep terminal text readable across light and dark themes using xterm's cell-aware minimum contrast handling.
- Share Terminal `New output` read state across devices and poll only while unread discovery is still needed.

### Fixed

- Refresh shared Terminal activity after live output, reconnects, and returning to an idle tab without repeatedly polling an already-unread Workspace.
- Preserve very fast File Run completion when a reused tmux pane exits before its atomic completion record becomes visible.

## [0.3.11] - 2026-08-21

### Added

- Scroll through tmux history and the live terminal as one native browser surface on desktop and mobile, with an exact `LIVE` return and preserved tmux copy-mode and mouse-reporting TUI behavior.
- Use the official xterm Unicode 11 width provider so ASCII, combining marks, Hangul, CJK, and other terminal code points use a stable cell-width table without Termroom-specific character heuristics.

### Changed

- Count connected terminal devices instead of counting every tab from the same browser session, while keeping input ownership and resize coordination per live client.
- Match mobile Ctrl helper keys to xterm control and modified-arrow sequences and recover transient mobile WebSocket disconnects without reopening permanently closed terminals.

### Fixed

- Keep terminal history refreshes lightweight during idle and output bursts, preserve the exact reading position during delayed responses, and return to the true live bottom after layout changes.
- Restore Workspace unregister controls in Remote Computer lists and return to the same computer after removal.
- Redirect stale terminal URLs to a valid terminal and normalize remote non-empty-directory errors without breaking deletion of unreadable empty directories.

## [0.3.10] - 2026-08-19

### Fixed

- Keep the mobile Workspace CPU usage popover centered within the viewport at every supported screen width.
- Preserve confirmed fast File Run exits when the atomic completion record is not yet observable.

## [0.3.9] - 2026-08-19

### Added

- Unregister Workspaces and unused local folder locations without deleting their folders or tmux sessions.

### Changed

- Keep Activity, File Run, and notification history bounded with periodic retention cleanup.
- Make local folder selection and mobile terminal helper keys fit their available screen space more naturally.

### Fixed

- Keep unavailable registered folders visible and recoverable instead of failing the Workspace browser.
- Prevent Workspace removal while Remote Runs still depend on it, and clean up detached runtime metadata safely.

## [0.3.4] - 2026-08-18

### Fixed

- Preserve a confirmed File Run program exit of 127 when its completion record is not observable after runtime preparation.

## [0.3.3] - 2026-08-18

### Fixed

- Skip unreadable directories while loading recent remote paths instead of failing the entire list.
- Preserve fast File Run output across tmux versions and explain rejected Remote Run snapshots.
- Preserve system TLS defaults for anonymous HTTPS Git clones used by Remote Runs.

### Tests

- Stabilize Remote Run observer timing and align regression coverage with the shipped behavior.

## [0.3.2] - 2026-08-17

### Fixed

- Render HTTPS asset URLs correctly behind standard reverse proxies such as SWAG without trusting forwarded client IPs.

## [0.3.1] - 2026-08-17

### Fixed

- Allow Core-to-Node HTTP/WS connections on operator-controlled encrypted private networks such as Tailscale.
- Support direct tailnet binding and secure cookies behind HTTPS reverse proxies without forcing one transport policy.

## [0.3.0] - 2026-08-17

### Added

- Connect outbound-only Linux computers as approved Termroom Nodes without opening SSH or an inbound port.
- Save up to three explicit Workspace commands and run them from the Workspace root in managed terminals.
- Open regular files in persistent tmux-backed Vim, Neovim, or Vi sessions directly from Files.
- Collect Remote Run result archives and safely preview and apply eligible text-file changes back to a Workspace.
- Install Termroom as a PWA with responsive app icons and browser-specific installation guidance.

### Changed

- Simplified the Docker setup so one `.env` selects the Core or Node service and one Compose file covers both modes.
- Reduced terminal font startup cost by splitting the bundled Nerd Font into smaller unicode-range assets.
- Reworked Workspace navigation, Run placement, connection feedback, settings overlays, and responsive mobile controls.
- Tightened local Workspace policy so container deployments can expose only SSH and Termroom Node computers.

### Fixed

- Made SSH and Node connection attempts show immediate progress and recover cleanly from disconnects and stale state.
- Preserved tmux terminal behavior across reconnects, Vim alternate-screen sessions, IME input, and different device sizes.
- Hardened password-file permissions, credential handling, Node enrollment, Remote Run result boundaries, and source conflict checks.
- Added pull-request CI, recursive browser JavaScript checks, Docker mode validation, and real systemd user-service E2E coverage.
