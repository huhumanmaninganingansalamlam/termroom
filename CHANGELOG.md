# Changelog

All notable changes to Termroom are documented in this file.

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
