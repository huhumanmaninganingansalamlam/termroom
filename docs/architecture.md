# Termroom architecture

## Overview

Termroom is a single-user self-hosted Core that exposes project-scoped terminal
and file access in a browser.

```text
Browser / PWA
      │
      ▼
┌────────────────┐
│ Termroom Core  │
├────────────────┤
│ Workspace DB   │
│ Auth / CSRF    │
│ Terminal ctrl  │
│ File service   │
│ SSH backend    │
└──────┬─────┬───┘
       │     │
 Local│     │SSH
       ▼     ▼
 filesystem  SFTP
 tmux        remote tmux
```

SSH hosts require normal SSH and tmux.

## Data model

### Computer

The local machine is implicit. SSH computers are stored with connection metadata,
authentication mode, and pinned host-key material.

### Local root

One Core can register multiple local roots. This supports projects across home
directories, mounted disks, or separate project trees without pretending that a
computer has one global workspace root.

Before a new local root is registered, the Open Workspace UI can browse real
directories starting from the Core user's home directory. This pre-Workspace
picker only lists directories, skips symlinks, and still requires an explicit
selection before the directory becomes a registered root. Absolute path entry
remains available for advanced users.

### Workspace

A Workspace belongs to one local root or one SSH computer and represents exactly
one project directory. It owns a stable tmux session identifier and last-opened UI
state.

For SSH computers, the pre-Workspace picker uses SFTP to browse real remote
directories starting from the SSH user's home directory. It skips symlinks and
does not create a Workspace until the user explicitly opens the selected folder.

### Terminal

Terminals map to tmux windows. Browser WebSockets attach to PTYs connected to the
same tmux window. tmux, not the browser, owns process persistence.

## Request layers

### Authentication

`TERMROOM_PASSWORD` produces a stateless signed browser session. Login failures are
rate-limited. Session cookies are HTTP-only and can be marked Secure behind HTTPS.

### CSRF

State-changing forms use a secret-derived CSRF token. Browser streaming-upload
requests send the same proof in `X-Termroom-CSRF`.

### Path boundaries

Every local file operation resolves inside the Workspace root. Symlink escapes are
rejected. If the Termroom config directory is inside a Workspace, that subtree is
hidden and blocked even when addressed directly.

Remote path operations use SFTP path normalization and refuse symlink traversal for
exposed operations.

## Terminal pipeline

### Local

```text
xterm.js
↕ WebSocket
PTY helper process
↕
tmux attach
↕
shell / TUI
```

### SSH

```text
xterm.js
↕ WebSocket
local PTY
↕ OpenSSH client
↕
remote tmux
↕
remote shell / TUI
```

The Core removes `TERMROOM_PASSWORD` from tmux and spawned SSH environments.

Viewport resize is bounded and the most recently active browser controls terminal
dimensions. Mobile IME composition stays on xterm's input path; Termroom does not
rebuild Korean/Japanese/Chinese composition itself.

## File pipeline

Local files use normal filesystem APIs; SSH files use SFTP. Both are adapted to the
same browser contract:

- list/stat
- bounded text read/edit
- upload with conflict protection
- create/rename/delete
- raw/preview/download
- multi-select archive
- current-directory search

ZIP generation streams source files into a temporary archive with a file-count
bound. Local archive traversal reuses path-boundary checks; SSH archive traversal
uses SFTP and refuses unsupported objects.

Large previews are bounded to prevent the browser from freezing. Remote media can
serve byte ranges where useful.

## Recent scan

Local scanning is bounded by file count and wall time. Remote scanning uses a
bounded `find`. Common dependency/cache directories are excluded. Projects may add
`.termroomignore`.

Recent does not infer command→file causality.

## SSH security

The first connection probes the actual host key. The user confirms the fingerprint,
then Termroom pins the key in its private known-hosts file. Later changes fail
closed.

Password mode stores encrypted credentials under the config directory. Managed-key
mode creates one Ed25519 key and reuses it. Existing private-key paths remain an
advanced option.

## Persistent state

New installs default to:

```text
~/.config/termroom/
├─ .env                  # optional password / default UI locale
├─ termroom.sqlite3
├─ access-token
├─ credential-key
├─ credentials/
└─ ssh/
```

Password resolution is explicit argument → `TERMROOM_PASSWORD` environment →
config-directory `.env` → project `.env` compatibility fallback. Any `.env`
that supplies the Termroom password must be owner-only.

Initial UI locale resolution is `termroom_locale` browser cookie → configured
`TERMROOM_LOCALE` (`en` or `ko`) → English. The environment variable takes
priority over config-directory and project `.env` values. Browser selection stays
per-browser and intentionally overrides the operator default.

`core.json` also records a content fingerprint of the installed Termroom package.
Before reusing a running Core, `termroom .` compares that fingerprint with the
package currently on disk. If the code changed, Termroom restarts only the web
Core and leaves Workspace tmux sessions alive. A running Core also detects package
file changes and returns a restart-required page instead of mixing newly installed
templates/static files with Python modules from the older process.

## UI architecture

The navigation model is:

```text
Home
→ Open Workspace
→ Computer
→ Workspace
→ Terminal | Files | Recent
```

Mobile uses a bottom tab bar for the three Workspace sections. Desktop uses a
sidebar with the same three destinations. Actions for the active section stay in
that section's toolbar instead of becoming extra navigation destinations.

The CSS source of truth is `termroom/static/app.css`. Responsive authority stays
in this one file.

## Deployment

Default bind is loopback. Other-device access should use LAN, an existing VPN such
as Tailscale, or an operator-managed HTTPS reverse proxy.

Docker uses a non-root `termroom` runtime user, `/config` for persistent state, and
`/workspaces` for allowed local content.

## Localization

Locale JSON lives under `termroom/locales/`. User-facing cookies and product copy
use Termroom names.
