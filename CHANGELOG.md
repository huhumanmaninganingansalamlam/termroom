# Changelog

All notable changes to Termroom are documented in this file.

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
