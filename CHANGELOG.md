# Changelog

All notable changes to Termroom are documented in this file.

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
