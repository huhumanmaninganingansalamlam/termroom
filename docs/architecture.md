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
│ Node control   │
└────┬─────┬─────┘
     │     │
Local│     │Remote
     ▼     ├─ SSH → SFTP + remote tmux
filesystem └─ Node outbound WSS → remote filesystem + tmux
tmux
```

SSH Remotes require normal SSH, `/bin/bash`, and tmux. Node Remotes require the
Termroom package, `/bin/bash`, and tmux, but no inbound port.

## Data model

### Computer

The local machine is implicit. Remote computers share one model with an internal
`ssh` or `node` connection method, normalized connection state, capabilities, and
last-seen data. SSH stores authentication and pinned host-key material. Node stores
the approved public identity while the private key and filesystem policy stay local
to the Node computer.

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

A Workspace belongs to one Local root or one Remote computer and represents exactly
one project directory. It owns a stable tmux session identifier and last-opened UI
state.

For SSH computers, the pre-Workspace picker uses SFTP to browse real remote
directories starting from the SSH user's home directory. It skips symlinks and
does not create a Workspace until the user explicitly opens the selected folder.

The same folder browsers may create one new child directory. Project creation is deliberately
not a new project system: validate one folder name, create the directory, then pass it through
the existing Workspace registration and tmux preparation path. If later Workspace preparation
fails, Termroom keeps the created directory rather than rolling back possible user data.

Node folder browse, project creation, Workspace registration, and Files operations are fixed
typed operations that revalidate the Node-local `allowed_roots`. SSH and Node still render the
same Workspace routes and UI.

### Remote Run

A Remote Run targets one registered Remote with the `remote_run` capability. It copies one
Local, SSH, or capable Node Workspace folder, public HTTPS Git repository, or ZIP into a
Termroom-managed remote directory and executes one command there.

```text
Remote Run
├─ selected SSH or Node Remote
├─ Workspace | public HTTPS Git | ZIP Source
├─ command
├─ temporary remote work directory
├─ one remote tmux session
├─ preparing | running | finished | stopped | failed | lost
├─ exit code
└─ one transient Workspace using the existing Terminal and Files UI
```

There is no Local Run target, worker, queue, scheduler, environment builder, or generic job
engine. The transient Workspace is excluded from Recent Workspaces and is deleted with the Run.
Remote tmux owns execution persistence when the browser or Core is gone. The Core observes
active Local/SSH runs with bounded backoff; Node reports completion and reconciles active runs
on heartbeat/reconnect.

### Terminal

Terminals map to tmux windows. Browser WebSockets attach to PTYs connected to the
same tmux window. tmux, not the browser, owns process persistence.

### File Run

A File Run belongs to one persistent Workspace and claims its single managed `file_run` slot.
The versioned server-owned Runner Registry resolves Python 3, Node.js, Bash, or an executable
shebang into structured argv. The browser cannot supply a runner or arbitrary command. The
managed tmux window keeps interactive I/O and the current or previous output visible while the
DB stores idempotency, source digest, exact argv, lifecycle state, exit code, and Event outcome.

### Event and Activity

Events are durable, deduplicated records created with File Run, Remote Run, and Remote connection
state transitions. Activity lists them newest-first with per-browser read state and safe target
links. Browser notifications use a device-and-Event claim so multiple tabs do not display the
same Event repeatedly. Event payloads exclude commands, output, credentials, and private paths.

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

### Node

```text
xterm.js / Files / managed Run UI
↕ authenticated browser routes
Termroom Core
↕ outbound WSS control + bounded typed streams
Termroom Node
↕
remote filesystem / PTY / tmux
```

Pairing uses a short-lived one-time code followed by explicit fingerprint approval. Node keeps
its private key, `allowed_roots`, and `run_root` in owner-only local state. Requests carry an ID,
operation type, deadline, and protocol version; fixed operations revalidate capability, identity,
canonical path, and managed ownership on the Node before acting.

The Core removes `TERMROOM_PASSWORD` from tmux and spawned SSH environments.

Viewport resize is bounded and the most recently active browser controls terminal
dimensions. Mobile IME composition stays on xterm's input path; Termroom does not
rebuild Korean/Japanese/Chinese composition itself.

## File pipeline

Local files use normal filesystem APIs, SSH files use SFTP, and Node files use fixed typed
streaming operations. All are adapted to the same browser contract:

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

Remote Run files reuse the existing SSH or Node Workspace primitives after the Source has been
materialized. The remote temporary filesystem is the source of truth.

## Remote Run lifecycle

Remote Runs target registered SSH or compatible Node Remotes. A Source is one Local, SSH, or
compatible Node Workspace folder, a public anonymous HTTPS Git URL, or one verified ZIP. Each
target stores the Run under
`$HOME/.cache/termroom/runs/<run-id>/` by default, with `work/` for the copied Source and results
and a hidden `.termroom/` directory for the marker, command, state, and logs.

The command is written to metadata and executed by a fixed Bash runner. User text is never
interpolated into a tmux command string. The Run is not a sandbox and uses the registered
Remote user's permissions. Public Git clone arguments and environment are fixed and isolated; private
Git authentication is intentionally unsupported.

Once `work/` is committed, the Run is linked one-to-one to a transient Remote Workspace row. Its
fixed `termroom-run-<uuid>` session exposes the running command and an interactive shell window.
All Terminal WebSocket, Files, preview, edit, upload, and download routes are the existing `/w/`
Workspace routes. Transient rows are excluded from Recent Workspaces.

Completed and stopped Runs expire after 24 hours. An uploaded ZIP that never starts is also
bounded by spool retention. Deletion is allowed only when the path is under the managed Runs root
and `.termroom/marker` matches the Run ID. SSH and Node cleanup follow the same root-and-marker
rule at the execution location. No periodic cleanup worker exists; cleanup runs at Core startup
and relevant list/browse operations, and users may delete a Run immediately.

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

## Node identity and service

Node creates its own keypair and sends only its public identity for fingerprint approval. The
Core rejects revoked or protocol-incompatible Nodes and never reuses the login password, browser
session, or SSH credentials as Node identity. Non-loopback Pairing and control require verified
HTTPS/WSS; there is no certificate-verification bypass.

`termroom node` can run in the foreground or as one product-owned systemd user service. The
service uses an absolute package entrypoint, a singleton process lock, the existing owner-only
identity, and bounded reconnect with jitter. Installing or removing the service does not modify
system lingering, elevate privileges, or discard Pairing state.

## Workspace usage

Workspace usage is a bounded estimate derived from tmux pane processes and observable
descendants. CPU, memory, and process count are reference data, not precise
accounting. Stale or failed samples are shown as unavailable with the last check time instead of
being reused as current values.

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

A Node uses the same configurable base directory with owner-only `node.json`, private identity,
runtime status, and lock files. Node-local allowed roots and managed run root are never stored as
Core-editable policy.

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
├─ Open Workspace → Local or Remote → Workspace → Terminal | Files | Recent
├─ Activity → exact Run, Workspace file, or Remote target
└─ Remote Run → Source → capable SSH/Node Remote → temporary Workspace
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
`/workspaces` for allowed local content. Release container images are built only
after the matching PyPI version is published; the Dockerfile installs that exact
PyPI package rather than copying the repository source. The release workflow then
publishes multi-architecture images to GHCR so Python and container artifacts share
one versioned source of truth.

## Localization

Locale JSON lives under `termroom/locales/`. User-facing cookies and product copy
use Termroom names.
