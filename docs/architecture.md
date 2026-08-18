# Termroom architecture

## Overview

Termroom is a single-user self-hosted Core that exposes project-scoped terminal
and file access in a browser. A persistent Workspace can run a small set of
user-saved commands, and a one-shot Remote Run can send a snapshot to another
computer and return its results without becoming a continuous sync system.

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
`ssh` or `node` connection method, capabilities, last-seen data, and four small
user-facing states: unchecked, connecting, available, and unavailable. The UI also
keeps the last successful connection time and an inline retry action; it does not
turn routine availability changes into a connection-history dashboard. SSH stores
authentication and pinned host-key material. Node stores the approved public
identity while the private key and filesystem policy stay local to the Node computer.

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
one project directory. It owns a stable tmux session identifier, last-opened UI
state, and up to three commands explicitly saved by the user. Saved commands always
run from the Workspace root; package manifests and frameworks do not create commands
implicitly.

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
├─ one transient Workspace using the existing Terminal and Files UI
├─ result ZIP
└─ conflict-safe collection when the Source is a Workspace
```

There is no Local Remote-Run target, worker, queue, scheduler, environment builder, or generic
job engine. The transient Workspace is excluded from Recent Workspaces and is deleted with the
Run. Remote tmux owns execution persistence when the browser or Core is gone. The Core observes
active Local/SSH runs with bounded backoff; Node reports completion and reconciles active runs
on heartbeat/reconnect. Download never mutates the Source. Collection is a separate explicit
operation and is available only when the Source is a persistent Workspace.

### Terminal

Terminals map to tmux windows. Browser WebSockets attach to PTYs connected to the
same tmux window. tmux, not the browser, owns process persistence.

All attached views share one tmux pane character grid. Its rows and columns are determined by
the viewport that most recently sent real user input. Connecting, focusing, reloading, or
passively resizing another view does not change that shared grid.

### Workspace Run

A persistent Workspace stores up to three user-entered commands; having none configured is a
normal state. The Workspace header exposes
a visible Run action; choosing a command starts it from the Workspace root in a managed tmux
window. Start requests are idempotent, and reconnecting opens the same managed run instead of
starting a duplicate. Termroom does not infer commands, install runtimes, or store a general
argument/environment/task configuration.

### File Run

A File Run belongs to one persistent Workspace and claims its single managed `file_run` slot.
The versioned server-owned Runner Registry resolves Python 3, Node.js, Bash, or an executable
shebang into structured argv. The browser cannot supply a runner or arbitrary command. The
managed tmux window keeps interactive I/O and the current or previous output visible while the
DB stores idempotency, source digest, exact argv, lifecycle state, and exit code. File Run stays
a narrow current-file shortcut and does not replace explicit Workspace commands.

### Run outcomes and Activity

File Run and Remote Run outcomes can create durable, deduplicated records. Activity is an
auxiliary return path to those runs, not an availability history or monitoring subsystem.
Browser notifications may claim a run outcome once per device, but their payloads exclude
commands, output, credentials, and private paths. The Workspace and Run screens remain the
authoritative current state.

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
operation type, relative admission budget, and protocol version. The Node measures that budget
on its monotonic clock while a request waits for a concurrency slot, avoiding Core/Node wall-clock
skew. Once a synchronous filesystem or tmux operation starts, it returns its real result rather
than a false timeout while work continues. Fixed operations revalidate capability, identity,
canonical path, and managed ownership on the Node before acting.

The Core removes `TERMROOM_PASSWORD` from tmux and spawned SSH environments.

Viewport resize is bounded and only the view that most recently sent real user input controls
terminal dimensions. Mobile IME composition stays on xterm's input path; Termroom does not
rebuild Korean/Japanese/Chinese composition itself. Vim/Neovim and other TUIs use the
same alternate-screen, cursor-mode, mouse, and bracketed-paste path. Reattach asks tmux
to redraw the current screen rather than reconstructing application state in the browser.
The baseline is `xterm-256color` with a bundled Nerd Font; true color is used only when
the complete browser/tmux/terminfo path supports it.

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
- validated file-to-terminal editing through a dedicated tmux window. Local and SSH call the
  backend directly; Node advertises `terminal_editor` and handles the fixed
  `terminal.editor.open` operation. The file path is transported as data, never evaluated as a
  browser-supplied shell command, and a live window is reused by normalized-path digest.

ZIP generation streams source files into a temporary archive with a file-count
bound. Local archive traversal reuses path-boundary checks; SSH archive traversal
uses SFTP and refuses unsupported objects.

Large previews are bounded to prevent the browser from freezing. Remote media can
serve byte ranges where useful.

Remote Run files reuse the existing SSH or Node Workspace primitives after the Source has been
materialized. The remote temporary filesystem is the source of truth until the user explicitly
downloads or collects a result. A download streams a ZIP without mutating the Source.

Workspace collection uses a three-way comparison:

```text
start snapshot digest
├─ current Remote result → added / modified / deleted in the Run
└─ current Source        → unchanged / changed since the Run started
```

Only added or modified regular files within the browser text-edit limit, encoded as UTF-8 without
NUL bytes, and whose Source path matches the reviewed state are applicable. Added files must be
inside a Source directory that already exists; collection does not create Source directories. A
path changed on both sides, or newly created on both sides, is a conflict. Remote deletions are
reported but never applied to the Source. Every backend revalidates its Workspace boundary, file
type, and digest immediately before writing through a temporary file. Existing paths use atomic
replace; additions use no-clobber creation and fail rather than replace a concurrently created
path. This is optimistic conflict detection, not filesystem compare-and-swap: an external editor
or Git process can still write between the final check and publication. Binary, oversized,
non-UTF-8, newly nested, and other skipped results use the result ZIP and manual merge path.
Local, SSH, and Node Sources implement the same contract; Remote-to-Remote collection streams
verified files through the Core.

## Remote Run lifecycle

Remote Runs target registered SSH or compatible Node Remotes. A Source is one Local, SSH, or
compatible Node Workspace folder, a public anonymous HTTPS Git URL, or one verified ZIP. Each
target stores the Run under
`$HOME/.cache/termroom/runs/<run-id>/` by default, with `work/` for the copied Source and results
and a hidden `.termroom/` directory for the marker, command, state, and logs.

A Workspace snapshot manifest records normalized paths, file types, executable bits, sizes,
and regular-file content digests. Common dependency/cache directories, virtual environments,
Git metadata, secret configuration, escaping symlinks, and special files are excluded. These
digests are the immutable comparison base for later collection; size and mtime alone are not
used as overwrite proof.

The command is written to metadata and executed by a fixed Bash runner. User text is never
interpolated into a tmux command string. The Run is not a sandbox and uses the registered
Remote user's permissions. Public Git clone arguments and environment are fixed and isolated; private
Git authentication is intentionally unsupported.

Once `work/` is committed, the Run is linked one-to-one to a transient Remote Workspace row. Its
fixed `termroom-run-<uuid>` session exposes the running command and an interactive shell window.
All Terminal WebSocket, Files, preview, edit, upload, and download routes are the existing `/w/`
Workspace routes. Transient rows are excluded from Recent Workspaces.

Result ZIP remains available for any terminal Run state while its managed folder exists and the
result stays within configured entry-count, directory-depth, and byte limits, including failed,
non-zero-exit, and lost outcomes. Unsupported filesystem objects are not archived. Public Git and
uploaded ZIP Sources have no writable Source Workspace and therefore offer download only. A
Workspace Source also offers a preview and explicit apply step; it never applies as part of
command completion.

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
session, or SSH credentials as Node identity. Pairing and control support HTTP/WS when the
operator has selected an encrypted private network such as Tailscale, while HTTPS/WSS remains
the safe choice on networks where transport confidentiality is not otherwise provided. Private
CA paths are verified for HTTPS and there is no certificate-verification bypass.

`termroom node` can run in the foreground or as one product-owned systemd user service. The
service uses an absolute package entrypoint, a singleton process lock, the existing owner-only
identity, and bounded reconnect with jitter. Installing or removing the service does not modify
system lingering, elevate privileges, or discard Pairing state.

## Supporting Workspace estimate

Workspace usage is a bounded estimate derived from tmux pane processes and observable
descendants. CPU, memory, and process count are reference data, not precise
accounting. Stale or failed samples are shown as unavailable with the last check time instead of
being reused as current values. It is a small Workspace-menu detail, not a primary navigation
surface, alert source, resource limit, or monitoring history.

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
├─ Open Workspace → Local or Remote → Workspace
│                                     ├─ Run (up to 3 explicit commands)
│                                     └─ Terminal | Files | Recent
└─ Remote Run → Source → capable SSH/Node Remote → temporary Workspace
                → Terminal | Files | Recent → result ZIP | collect changes
```

Mobile uses a bottom tab bar for the three Workspace sections. Desktop uses a
sidebar with the same three destinations. Actions for the active section stay in
that section's toolbar instead of becoming extra navigation destinations.

Activity remains a small global shortcut to retained File Run and Remote Run outcomes. It does
not own Remote availability state or add a monitoring destination to the Workspace hierarchy.

The Settings menu owns PWA installation discovery. On a secure context, a captured native
install prompt is opened only after the user chooses Install Termroom. Installed standalone
views hide that action. Browsers without a prompt API receive short browser-specific guidance;
iOS Safari points to Share → Add to Home Screen. No install banner or automatic prompt is used.
The service worker does not cache authenticated Workspace, file, terminal, or Run responses.

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

The official image provides the product runtime plus Bash, Python 3, tmux, and Neovim. Project
runtimes and dependencies belong to an operator-built derived image or host environment; Node
pairing must not infer manifests, install packages, or run arbitrary initialization commands.
A future devcontainer-style integration should place the Termroom Node agent inside or alongside
the selected execution environment instead of mutating that environment after pairing.

## Localization

Locale JSON lives under `termroom/locales/`. User-facing cookies and product copy
use Termroom names.
