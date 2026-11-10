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

SSH Remotes may run Linux or macOS and require normal SSH, `/bin/bash`, and tmux.
Termroom queries the authenticated account's configured login shell once for its exported
absolute command-search entries, isolates startup output from the protocol, and caches the
sanitized result for subsequent `/bin/sh` operations. Tool availability is still verified
with command lookup; no Homebrew, package-manager, or executable directory is guessed. Node
Remotes remain Linux computers and require the Termroom package, `/bin/bash`, and tmux, but
no inbound port.

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
Recent-file traversal also uses bounded SFTP operations rather than GNU `find`, so the
same Files behavior works on Linux and macOS SSH hosts.

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

User activation selects one input controller; other views remain observers. Admitted input
finishes before control transfer, and only the controller's real input owns grid size.
Passive resize/reconnect cannot steal control. Mobile IME composition stays on xterm's input path; Termroom does not
rebuild Korean/Japanese/Chinese composition itself. Vim/Neovim and other TUIs use the
same alternate-screen, cursor-mode, mouse, and bracketed-paste path. Reattach asks tmux
to redraw the current screen rather than reconstructing application state in the browser.
The baseline is `xterm-256color` with a bundled Nerd Font; true color is used only when
the complete browser/tmux/terminfo path supports it.
Canonical pane controls also identify a foreground shell outside tmux copy mode.
In normal-screen shell state, xterm does not generate mouse reports left enabled by
an exited TUI; keyboard and pasted text are not filtered. TUI and copy-mode mouse
handling stays on the native path.

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

Local scanning is bounded by file count and wall time. SSH Recent uses bounded SFTP
directory traversal, skips symlinks and common dependency/cache/hidden directories,
honors `.termroomignore`, and reports truncated results when its file-count or wall-time
limit is reached. Node Recent goes through the Node `files.recent` operation and its
local file-service bounds. Projects may add `.termroomignore`.

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

The CSS source of truth is `termroom/static/app.css`.

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

## Localization

Locale JSON lives under `termroom/locales/`. User-facing cookies and product copy
use Termroom names.

## UI-independent application actions (v1 foundation)

This section traces the versioned credential/action foundation and the
read-only exploration checkpoint, followed by scoped mutation slices. It is not
a release or deployment certificate; isolated tests do not prove a live user's
desktop session or arbitrary client compatibility. The 2026-11-07 human-first
direction and security exclusions remain authoritative. Each action extends
**existing managers**, not a second business-logic implementation.

### Trust boundary and credential management

A browser session is a signed HTTP-only cookie. A non-UI client uses an independent,
revocable bearer secret in the HTTP Authorization header; the bearer secret does not
create a browser session, authenticate a WebSocket, or bypass CSRF/Origin controls.
No credential is accepted in a query parameter or request body on control routes.
Do not put credentials in logs, URLs, terminal history, or committed files.

The owner signs in through the existing browser login, then uses these browser-only
management endpoints with the existing X-Termroom-CSRF proof on writes:

| Method | Path | Effect |
| --- | --- | --- |
| GET | /api/client-credentials | List up to 200 credential metadata records and 200 current Workspace name/ID pairs |
| POST | /api/client-credentials | Create one credential; return its bearer token **once** |
| POST | /api/client-credentials/{id}/revoke | Revoke the exact ID with JSON confirmation |

Creation accepts JSON (max 8,192 bytes):

\`\`\`json
{
  "name": "My automation",
  "capabilities": ["read"],
  "workspace_ids": ["<existing-workspace-id>"]
}
\`\`\`

The capability list defaults to ["read"] when omitted. An operator must
explicitly include "operate" alongside "read" to grant it. There is no
implicit all-Workspace grant; choose 1–32 distinct *persistent* Workspace IDs.
Names are bounded to a single line of 1–80 characters. There are at most 32
active credentials. A revoked credential remains visible as metadata.
The verifier is a SHA-256 digest of a high-entropy token, never its plaintext.
Only the creation response contains \`client.token\`; subsequent list, status,
and revoke responses omit it. \`last_used_at\` is updated at most once per minute.
If a Workspace is removed, an old grant cannot address a replacement by label.

Revocation requires an authenticated browser, the CSRF header and the exact
JSON \`{"confirm_client_id":"<id>"}\`. It prevents all *subsequent* control
requests; it does not kill an already running OS process. A browser session
alone never authenticates the non-UI routes.


A request already authenticated before revocation can be waiting on the
Terminal's admission lock or a remote-pane check. Before taking control,
dispatching input, renaming a Terminal, or executing a human-approved close,
the server rechecks its credential. A revoked client receives a recorded
`409 credential_revoked` refusal without that mutation. Once an input or
Terminal mutation is dispatched, token revocation does not cancel the admitted
operation or kill an existing OS process. Fresh requests with the revoked
token receive 401. This follows the revoked-token rejection principle
of [RFC 6750 §3.1](https://www.rfc-editor.org/rfc/rfc6750.html#section-3.1);
the pending-action receipt is Termroom-specific, not an OAuth compliance claim.

Reusable bearer secrets are accepted over HTTPS. HTTP is allowed only when both
the request host and direct peer are loopback. A private-network HTTP connection
is **not** treated as encrypted. The operator must configure a trustworthy HTTPS
reverse proxy and keep token material on trusted clients; do not infer secure
transport from a private IP alone.

### Available non-UI v1 routes

Use \`Authorization: Bearer <one-time-token>\`. Each route returns JSON and no
stateful browser navigation, active Terminal selection, input ownership,
tmux resize, Run preparation, recent Workspace touch, or unread acknowledgement.

| Method | Path | Current read contract |
| --- | --- | --- |
| GET | /api/control/v1/capabilities | Version, exact grant, supported read actions and limits |
| GET | /api/control/v1/workspaces | Only granted persistent Workspaces (at most 32); connection status is known only when Node transport tracks it |
| GET | /api/control/v1/workspaces/{id} | Workspace identity; no project root path or secret fields |
| GET | /api/control/v1/workspaces/{id}/commands | Up to three stored commands, legacy slot and content digest |
| GET | /api/control/v1/workspaces/{id}/terminals | Up to 64 registered Terminal metadata entries; live status not verified |
| GET | /api/control/v1/workspaces/{id}/terminals/{terminal_id} | Exactly one registered Terminal metadata entry |
| GET | /api/control/v1/workspaces/{id}/terminals/{terminal_id}/snapshot?lines=100 | 1–200 lines; up to 64 KiB captured tmux pane text |
| GET | /api/control/v1/workspaces/{id}/files?path=. | Directory listing, up to 256 entries and 128 KiB metadata |
| GET | /api/control/v1/workspaces/{id}/files/stat?path=… | One bounded filesystem metadata observation |
| GET | /api/control/v1/workspaces/{id}/files/search?path=.&q=… | Bounded filename search, up to 100 matches |
| GET | /api/control/v1/workspaces/{id}/files/text?path=… | UTF-8 text, at most 128 KiB, content digest and mtime |
| GET | /api/control/v1/workspaces/{id}/file-runs[/{run_id}] | Persisted File Run list/detail; no live poll |
| GET | /api/control/v1/workspaces/{id}/workspace-runs[/{launch_id}] | Command launch admission receipts only, not inferred completion |
| GET | /api/control/v1/workspaces/{id}/remote-runs[/{run_id}] | Persisted Workspace-source Remote Run list/detail if Target Computer is also granted |
| GET | /api/control/v1/workspaces/{id}/remote-runs/{run_id}/snapshot?lines=100 | Existing Run-owned managed Terminal only; 1–200 lines, at most 64 KiB, no live Run poll or bridge creation |
| GET | /api/control/v1/actions/{action_id} | Client-bound action receipt, not an execution trigger |

\`workspace.describe\` returns ID, display name, backend kind and Computer ID,
plus a collection timestamp. It deliberately excludes roots, paths, passwords,
SSH keys, Node identity, private config and arbitrary file/terminal content.
A nonexistent Workspace and a real but unauthorized Workspace both return
\`404 resource_unavailable\`. A token is checked on every request, including
action-status lookup. No API-specific bypass is added to browser routes.

The new control routes use an envelope with \`ok\`, \`schema_version: 1\`, a
stable \`code\` for refusal, and action-specific fields. The status endpoint
does not expose action payloads or their digests. Authentication failures return
\`401 unauthorized\`, insecure transport \`426 secure_transport_required\`,
and inaccessible actions \`404 action_not_found\`. No public Swagger UI or
MCP/WebMCP adapter is enabled.

### Read-only observation semantics (Task T-20261107092838Z)

The API enforces the client's exact Workspace ID on each endpoint. Missing and
unauthorized resources share `404 resource_unavailable`; a pagination cursor
whose backing Run record is gone or no longer visible returns `410 page_expired`.
Run lists use deterministic `(created_at, id)` keyset pages (25 visible +
one lookahead) and are **best-effort**, not snapshot-isolated. No workspace list
pagination is needed because credential grants are capped at 32 Workspaces.
An over-limit directory or Terminal list fails explicitly with 413 rather than
silently returning the first arbitrary subset. Directory search also caps
backend traversal. The read gate admits at most four concurrent slow observations
with an eight-second response deadline; timeouts do not imply cancellation of
an already executing thread/SSH command.

Local and SSH Terminal snapshots call an observation-only `capture-pane`
path that does not `ensure_workspace`, attach, resize or claim input. A known
pane identity is checked before and after observation; on Node the existing typed
scrollback call is reused and pane generation is explicitly **unknown**.
A snapshot SHA-256 identifies captured content, **not** an ordered byte-event
stream, shell-command completion, xterm render state or a resume cursor.
Returned output is untrusted and may contain secrets that an authorized client
can already view. Raw terminal output is capped at 64 KiB. No action changes
the browser's selected Workspace, Terminal, draft, focus or scroll position.

File operations reuse the existing Local, SSH/SFTP or Node file owner and its
path/symlink checks; local private state is excluded on list, search and direct
read/stat. Read responses do not automatically redact secrets inside authorized
project files. Offline SSH/Node observations return an explicit unavailable
response; transport failure does not imply a run exited.

File Run and Remote Run statuses are the **persisted backend state** at
`collected_at`, not a newly reconciled live status. They preserve existing
preparing/running/finished/stopped/failed/lost values and exit codes without
calling a Run scheduler, poller, reaper or cleanup. Remote Run visibility needs
both Source Workspace scope and a granted persistent Remote Workspace on the
Target Computer; Git/ZIP-sourced Runs have no persistent Source Workspace grant
and are intentionally absent. Result review metadata, when saved, means only
that a prior report exists; it is never an approval to apply those changes.
Workspace Run has no durable lifecycle/outcome table: a launch claim means
only that the admission was recorded, not that a shell executed or completed.
A File Run's managed Terminal may be observed separately by exact ID when
still registered. Remote Run output can be captured through its persisted Run
ID only when both Source Workspace and Target Computer are granted and a
preexisting transient Run Workspace and matching managed Terminal are registered.
A missing bridge or owned Terminal returns 404; a failed read returns 503.
The 64 KiB snapshot does not create, repair or poll the Run Workspace and is
not an execution status, completion signal or resumable event stream.

This contract follows HTTP safe-read semantics (RFC 9110), bounded API pages
(GitHub REST), explicit invalidated cursors (Kubernetes API), and the
distinction between terminal text and shell execution signals (VS Code shell
integration). Reverse-proxy TLS identity must come from a trusted proxy;
the app does not treat an arbitrary peer's `X-Forwarded-Proto: https` as proof.
Read-path verification includes synthetic Local/permission/concurrency tests,
a real isolated sshd/SFTP/tmux host, and a real Node process. A separate
temporary-profile headless Google Chrome holds a terminal in Workspace A with
an unsent Korean draft and focused composer while two distinct bearer clients
concurrently read Workspaces B and C. The regression compares URL, selected
Terminal, draft and selection, focus, scroll, tmux geometry, controller, and
persisted Workspace/Terminal state before and after. Initial browser attach and
composer layout are allowed to settle **before** that baseline is captured;
their normal one-time resize must not be confused with a read-triggered resize.
Chrome's test-only profile is never the owner's normal browser profile. This
isolated real-browser test does not prove the state of a person's live desktop
session; that optional on-screen check needs explicit desktop availability and
consent, and was not performed.

**Historical T38 limit:** At the read-only checkpoint, the `operate`
capability had no mutation route. The Terminal mutation slice is documented
below; the other action-specific slices have separate owners and acceptance.

### Shared Terminal mutation and input contract (T-20261107092839Z)

The earlier T38 read-only acceptance above is historical. An explicitly granted
`operate` credential can now use the following Terminal-specific actions; no
client gets terminal-input authority from a read, an HTTP connection, the
browser's session cookie, or a generic `confirm=true` field. Only registered
persistent Workspace/Terminal IDs are addressable.

| Method | Suffix under `/api/control/v1/workspaces/{id}` | Required action identity |
| --- | --- | --- |
| GET | `/terminals/{terminal_id}/control` | Live canonical pane/mode and in-memory controller revision; no attach or resize |
| POST | `/terminals` | `action_id, name`; creates one shell Terminal |
| POST | `/terminals/{terminal_id}/rename` | `action_id, name, expected_tmux_window`; exact shell Terminal only |
| POST | `/terminals/{terminal_id}/control/claim` | `action_id, expected_tmux_window, expected_pane, expected_control_revision` |
| POST | `/terminals/{terminal_id}/control/release` | `action_id, expected_tmux_window, generation, expected_control_revision` |
| POST | `/terminals/{terminal_id}/input` | `action_id, command_id, expected_tmux_window, pane, generation, expected_control_revision, data, append_enter` |
| POST | `/terminals/{terminal_id}/close-intents` | `action_id, expected_tmux_window`; requests a browser review |
| GET | `/terminals/{terminal_id}/close-intents/{approval_id}` | Polls pending/approved/consumed, never approves |
| POST | `/terminals/{terminal_id}/close` | `action_id, expected_tmux_window, approval_id`; consumes matching human approval |

To share an existing Terminal, observe its exact pane and control revision.
The person currently controlling it uses **Terminal 입력권 넘기기** in the
Terminal manage menu; the browser confirms that unfinished text remains in
the same pane. A non-UI client then claims that observed pane/revision and
receives a private generation. A second client or active browser controller
gets `control_conflict`, never a queued write. Even after browser disconnect,
previous human input blocks a claim until a genuine browser handoff. A real
foreground Terminal activation can reclaim control after the current admitted
write completes, invalidating the non-UI generation. Reconnect, passive resize
and read do not silently steal that generation.

The `data` field is UTF-8 text (1–4,096 bytes); C0 controls other than
newline/tab, DEL, ESC, NUL and raw carriage return are refused. One input
action loads an action-unique tmux paste buffer and uses `paste-buffer -p`
for the exact pane. `append_enter: true` sends **one** separate Enter after
the paste, and `false` sends none. Text and Enter share the same per-Terminal
admission lock with browser input and control transfer. Non-UI read/claim/input
never changes rows/columns; existing human grid ownership rules remain.
Local/SSH/Node use their existing Terminal owner; an old Node without
`terminal_input_v1` reports unsupported rather than accepting an unsafe
fallback. A pane that ended, changed, or entered tmux copy mode refuses input
with `pane_ended`, `pane_changed`, or `pane_in_mode`; stale control refuses
with `control_conflict` or `control_revision_changed`.

Once admitted, an owned input write and a create, rename, or approved close
operation survive HTTP cancellation until the backend operation and receipt
recording finish. A strongly referenced in-flight task is retained until its
completion callback; `shield()` without this reference is insufficient.
Retrying the *same*
client/`action_id`/payload only returns its stored receipt; changing payload
with the same key returns `idempotency_conflict`. `accepted` with phase
`pty_delivered` proves tmux delivery, **not** shell execution, TUI readiness,
a clean prompt, or receiver-visible effects. Lost transport status is
`unknown`; clients query `/api/control/v1/actions/{action_id}` and never
automatically replay or send a compensating Enter. This is not durable
exactly-once process execution across crashes.

Closing can terminate live processes. A close intent is bound to the exact
credential, action, Workspace, Terminal and tmux window, expires after ten
minutes, and returns a browser-only `human_review_url`. A logged-in human
reviews the identity and approves through a CSRF-checked POST. The intent is
single-use and approval alone does not kill a process; the matching final
action must still pass the exact-identity check. Same-name sibling Terminals
are unaffected.

### Mutation identity and receipt vocabulary

Future mutations must call the same service/path safety owners as the human UI.
One action identity comprises:

- the authenticated client ID and client-supplied \`action_id\`;
- the action name, exact resource kind/ID and SHA-256 of the canonical payload;
- an admission-owner Core instance generation.

The SQLite receipt store uses the unique pair (client ID, action ID).
Claiming a new identity records \`accepted\` before dispatch. Repeating the
same identity and payload returns the same receipt and **does not dispatch**;
changing action/resource/payload with the same key yields
\`idempotency_conflict\`. There is no automatic queue, retry or replay. A
receipt has a 30-day observation horizon, but IDs remain as tombstones instead
of being silently reusable; the bounded 10,000-receipt/client capacity refuses
new work rather than evicting keys that may already have acted.

The initial receipt \`status\` is \`accepted\`. Other allowed statuses are
\`refused\`, \`observed\` and \`unknown\`. \`phase\` separately preserves the last
observed stage: \`accepted\`, \`dispatched\`, \`pty_delivered\`,
\`process_started\`, \`process_finished\` or \`result_verified\`.
The final phase must be backed by the corresponding real evidence. PTY delivery
or an HTTP response is **not** shell/TUI completion, and remote availability is
**not** process success. When the Core generation changes or an accepted
receipt passes its observation horizon without a known final outcome, lookup
returns \`unknown\` without deleting its prior phase or replaying the request.
A missing or lost network response never proves failure or success.

This first slice implements the receipt store and authenticated status read.
It does **not** expose a generic mutation submission endpoint. Later action
implementations must bind their own exact resource permissions, read/version
tokens, effects, stream revocation, deadlines and evidence to this ledger.

### Security and non-regression limits

- Read and operate are application capabilities, **not an OS sandbox**.
  An explicitly admitted shell command later runs with the selected OS user's
  normal authority; product endpoint confirmation cannot prevent all shell effects.
- Product-destructive operations (delete, force stop, result apply, etc.) remain
  unavailable here. A future action needs an exact, payload-bound human approval
  protocol; a client-supplied \`confirm=true\` is not authorization.
- A file/Terminal/Run path cannot be implemented by silently reading private DB
  state or driving a person's browser. Both clients must use the same operation
  owner and error semantics.
- Same-Terminal sharing needs controller ownership across partial typed input,
  not just per-frame byte locking. Reconnect/read never steals control.
- Status receipt retention is not durable exactly-once execution across every
  crash or external side effect. Unknown remains unknown until independently
  reconciled; no automatic second Enter or duplicate Run start is allowed.

The foundation's local tests use synthetic Workspaces and temporary SQLite
state. Real browser, SSH, Node, physical mobile and deployment acceptance are
separate future gates.

### Operating sequence for a non-UI client

1. The signed-in owner selects exact Workspace IDs and issues a credential with
   `read` (and `operate` only when needed). Copy its bearer token at creation;
   later management responses cannot recover the plaintext.
2. Send that token only in `Authorization: Bearer` over the trusted transport.
   First GET `/api/control/v1/capabilities`, then GET the granted Workspace,
   Terminal, command and Run lists to obtain exact resource identities.
3. Read `/workspaces/{id}/files/text?path=...` under `/api/control/v1` before
   a file PUT. Supply the returned digest and mtime with a fresh `action_id`;
   on `409 file_conflict`, keep the submitted draft in the client and inspect
   `current_version` before any deliberate retry.
4. For a named command or File Run, supply its exact ID/revision or file digest,
   plus fresh action/launch identities. After a lost response, GET
   `/api/control/v1/actions/{action_id}`; never make a new launch to guess an
   outcome. A Core restart can preserve the Run ID while leaving the action
   receipt `unknown`. Persisted Run reads are not live completion polls.
5. For an intentionally shared Terminal, GET its `/control` pane and revision.
   If a browser owns partially typed input, the person explicitly releases it;
   then claim control and send bounded input using the returned generation.
   `control_conflict` is not a delayed queue, and `pty_delivered` is not
   command completion. The non-UI client need not navigate or click the browser.

These actions reuse the same Local, SSH and Node operation owners as the human
UI. An operate credential is not a shell sandbox or permission to bypass
browser-bound approvals for destructive product operations.
