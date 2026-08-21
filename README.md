# Termroom

[English](https://github.com/huhumanmaninganingansalamlam/termroom/blob/main/README.md) · [한국어](https://github.com/huhumanmaninganingansalamlam/termroom/blob/main/README.ko.md)

**A personal workspace for continuing terminal work from a browser.**

Run `termroom .` inside a project folder to open its terminals and files in your
browser. `tmux` keeps terminal processes alive, so closing the browser or switching
devices does not end the work that is already running.

Termroom is aimed at workflows such as starting a long build or AI task on a laptop,
checking it from a phone later, running a small set of explicit project commands, and
working with remote Linux servers through the same terminal-and-files interface.

> **Status: early release.** Termroom is available as a Python CLI package and the
> project is still evolving quickly. Expect small workflow and UI changes between
> early versions.

## When is it useful?

- Check a long-running command from a phone or tablet after starting it on your PC.
- Keep terminal work alive even when the browser is closed.
- Browse, upload, download, or edit project files from the browser.
- Move between several local projects from one interface.
- Use SSH Linux servers and outbound-only Termroom Nodes in the same Workspace flow.
- Save up to three explicit commands per Workspace and run them from its root.
- Run the current Python, JavaScript, or Bash file without assembling its command by hand.
- Send a Workspace snapshot, public HTTPS Git repository, or ZIP to another Remote and
  recover its output and result files later.
- Download Remote Run results as a ZIP or review conflict-safe changes before bringing them
  back to the source Workspace.

Termroom is not a cloud IDE. **The real terminal processes and files stay on your Linux
computer or on a Remote Linux computer you control.** Termroom provides the browser interface.

## Quick start

### 1. Requirements

The current release requires:

- Linux
- Python 3.12+
- `tmux`

For example, on Ubuntu/Debian systems, install `tmux` if needed:

```bash
sudo apt install tmux
```

### 2. Install Termroom

For a CLI application, an isolated tool environment is the cleanest option. With
[`uv`](https://docs.astral.sh/uv/):

```bash
uv tool install termroom
```

Or with `pipx`:

```bash
pipx install termroom
```

Regular `pip` installation is also supported:

```bash
pip install termroom
```

After installation, the `termroom` command is available from your shell. `uv tool`
and `pipx` are recommended because they keep Termroom's dependencies isolated from
other Python applications.

> If your shell says `termroom: command not found`, run `uv tool update-shell` once and
> open a new terminal when you installed it with `uv tool`.

### 3. Open your project

Store the Termroom login password once in its global configuration:

```bash
mkdir -p ~/.config/termroom
printf '%s\n' 'TERMROOM_PASSWORD=choose-a-long-unique-password' 'TERMROOM_LOCALE=en' > ~/.config/termroom/.env
chmod 600 ~/.config/termroom/.env
```

Then move to the project you actually want to use:

```bash
cd ~/my-project
termroom .
```

Termroom starts in the background and opens your browser automatically. You can also
open the printed URL manually. Sign in with the password stored in
`~/.config/termroom/.env`.

Once a Termroom Core is already running on the same computer, another project can be
added with:

```bash
cd ~/another-project
termroom .
```

## What can it do?

### Terminal

- Use real shell and TUI programs in the browser.
- Keep work running in `tmux` after the browser closes.
- Create, rename, and close multiple terminals per Workspace.
- Reopen the same terminal after reconnecting.
- Use Vim/Neovim and other alternate-screen TUIs through the same real PTY and `tmux` path.
- Mobile CJK/IME input and touch helper keys.
- Optional command-editing mode for long commands.
- Browser-local terminal font-size settings.
- Search and copy existing `tmux` scrollback.

### Files

- Browse and search project folders.
- Create a new project folder from the Open Workspace browser and open it immediately.
- Select several files or folders and download them as a ZIP.
- Upload multiple files with overwrite confirmation.
- Create files/folders, rename, and delete them.
- Edit small text files directly in the browser.
- Open any regular file directly in a persistent tmux Vim session; Termroom prefers
  Neovim, then Vim/Vi, and reuses the live session for that file.
- Preview images/PDFs, JSON/CSV, and bounded portions of large text files.
- Use the same Files UI for local, SSH, and Termroom Node projects.

### Run Workspace commands

- Save up to three commands that you explicitly choose for a persistent Workspace.
- Start them from the visible Run action at the Workspace root and reopen the managed
  Terminal while they are running.
- Use the tools, runtime, virtual environment, and permissions already present on that
  computer.
- Termroom does not infer commands from package manifests or add argument, environment,
  task-graph, or Run Recipe configuration.

### Run the current file

- Save and run the current Python 3, Node.js, Bash, or executable-shebang file from the
  editor.
- Use one managed interactive Terminal per Workspace, with exact exit status, stop, and
  force-stop controls.
- Keep the active run attached to the saved file and recover its outcome after reconnecting.
- This is a narrow file shortcut; Termroom does not install runtimes or create environments.

### Remote Run

- Copy a Local, SSH, or compatible Node Workspace folder, a public HTTPS Git repository,
  or one ZIP to a registered SSH or compatible Node Remote's temporary space.
- Run one command with that Remote user's installed tools and CPU/GPU/RAM.
- Keep the command alive in a dedicated remote `tmux` session when the browser disconnects.
- Open the prepared folder through the normal Workspace Terminal and Files UI; there is no
  separate log dashboard or file viewer.
- Download the retained execution folder as a result ZIP, including after a failed command,
  when files remain within the configured entry, nesting, and size safety limits.
- For a Workspace Source, preview added, modified, conflicting, and skipped files before
  bringing eligible changes back. Apply is limited to small UTF-8 text files; a new file must
  stay inside an existing Source directory. Termroom rechecks the current Source immediately
  before publishing each change: existing files use atomic replace, while additions use
  no-clobber creation. Remote deletions are never propagated. Binary, oversized,
  non-UTF-8, newly nested, or otherwise skipped results remain available through the result ZIP
  for manual merge. This is optimistic conflict detection, not a lock on external editors or Git.
- Keep completed files for 24 hours, with immediate deletion available from the temporary
  Workspace header.
- Remote Run never configures environments, provides a sandbox, schedules jobs, continuously
  syncs, or writes changes back automatically.

### Recent

- See recently created or modified files.
- See recently used terminals and activity timestamps.
- Identify files that are still growing.
- Exclude dependency/cache/hidden directories by default.
- Add project-specific rules with `.termroomignore`.

Activity is a compact way to return to File Run and Remote Run outcomes; it is not a Remote
connection-history or monitoring dashboard. A Workspace menu may also show bounded CPU,
memory, and process-count estimates when they are available. Those values are reference data,
not resource accounting or alerts.

## The basic model

In Termroom, a **Workspace is simply one project folder**.

```text
Computer
└─ Workspace (project folder)
   ├─ Run (up to 3 explicit commands)
   ├─ Terminal
   ├─ Files
   └─ Recent
```

A `Computer` is either this Linux machine or a Remote Linux computer connected through SSH
or Termroom Node.

The Workspace settings menu can unregister a Workspace from Termroom without deleting its
project folder, files, tmux session, or running processes.

Activity and completed current-file Run history are retained for 30 days. Active Runs and
the Run attached to the current Run Terminal are retained until they are no longer in use.

**Remote Run is a temporary Workspace shell, not a persistent project.** Its Source is copied
to an SSH or compatible Node Remote, its Terminal and Files reuse the normal Workspace UI,
and its managed folder is disposable. It does not appear in Recent Workspaces.

### Local projects

The simplest path is to run Termroom from the project folder:

```bash
cd ~/projects/example
termroom .
```

The web UI can also open other allowed folders on this computer as Workspaces.

From the folder browser, choose **New project**, enter one folder name, and Termroom creates
that directory and opens it through the normal Workspace flow. No project template or
environment is generated.

Choose **Add location → Browse folders** to start from your home directory and click through
folders instead of typing an absolute path. Direct path entry remains available when you
already know it.

### SSH servers

Choose **Add SSH computer** in the web UI, then:

```text
Enter SSH address
→ verify the host-key fingerprint
→ choose password / Termroom-managed key / existing key authentication
→ browse from the remote home folder or type the project path directly
→ open the Workspace
```

The remote folder browser has the same **New project** action. The directory is created via
SFTP and then opened as an ordinary remote Workspace.

The remote Linux host needs an SSH server, `/bin/bash`, and `tmux`.

### Termroom Node

Use Termroom Node when a Remote Linux computer can make outbound HTTP(S)/WS(S) connections but
cannot expose an SSH server or inbound port.

```text
In the Core: Open Workspace → Connect computer → Connect with Node → Create pairing code
On the Remote: termroom node pair --core https://core.example --code <code> \
                 --allow-root /home/user/projects
In the Core: verify and approve the Node fingerprint
On the Remote: termroom node install-service
```

`--allow-root` is repeatable and stays under the Node user's local control. The Core cannot
expand allowed roots or the Node's managed Remote Run root. `install-service` installs and
starts a systemd user service; `termroom node status` reports both service and Core connection
state, and `termroom node uninstall-service` removes only the service while preserving the
Node identity and pairing configuration.

If the Core uses a private HTTPS CA, add `--ca-file /path/to/core-ca.pem` to `node pair`.
Termroom stores that verified path in the Node-local config and uses it for the control
connection too; certificate verification cannot be disabled.

The Core URL may use plain HTTP when Core and Node communicate through an operator-controlled,
encrypted private network such as Tailscale. Use HTTPS on an ordinary LAN or any network where
transport confidentiality is not otherwise provided. The selected HTTP or HTTPS scheme also
selects WS or WSS for the persistent control connection.

Once paired, a compatible Node uses the same Remote picker and Workspace Terminal, Files,
Workspace Run, File Run, Remote Run, result-recovery, and reconnect flows as SSH. Node
requires `/bin/bash` and `tmux`, but no inbound SSH connection.

#### Run a Node with Docker

The same official image and `compose.yaml` can run as a Node when you do not want to install
Termroom directly on the remote Linux computer. The Node service opens no inbound port. It keeps
configuration and Node identity in a named volume and exposes only the explicit host project
bind mount.

```bash
cp .env.example .env
chmod 600 .env
```

Change the single `TERMROOM_MODE` value in `.env` to `node` and set the remote-computer mount
path. `COMPOSE_PROFILES` follows that value and does not need a separate edit:

```text
TERMROOM_MODE=node
TERMROOM_WORKSPACES_HOST_PATH=/home/user/projects
```

With `TERMROOM_MODE=node`, the same image starts the Node process instead of the Core. Start the
long-running container first. An unpaired container stays running and waits for its configuration
instead of entering a restart loop:

```bash
docker compose up -d --remove-orphans
docker compose logs termroom-node
```

Create a Node pairing code in the Core, then pair from inside that same container:

```bash
docker compose exec -u termroom termroom-node \
  termroom node --config-dir /config/node pair \
  --core https://termroom.example \
  --code <10-minute-one-time-code> \
  --allow-root /workspaces \
  --name build-node
```

The pairing code expires after 10 minutes and is consumed when the pairing request is submitted,
so do not store it in `.env`. Pairing writes the identity and configuration to the named volume;
the waiting entrypoint detects it and starts the Node automatically without a restart. Verify and
approve the fingerprint shown in the Core, then follow the logs with:

```bash
docker compose logs -f termroom-node
```

`--remove-orphans` removes the previous mode's container when the same directory switches
between Core and Node.

Do not run `termroom node install-service` in Docker. Compose's `restart: unless-stopped`
manages the Node process. Terminals and commands run inside the Node container rather than
directly on the host. Build a Node-specific image from the official image when the Workspace
needs Git, Node.js, compilers, CUDA, or other additional tools. The Core URL must be reachable
from inside the container; HTTP is supported on an encrypted private network such as Tailscale.

The official image includes Bash, Python 3, tmux, and Neovim, and uses Bash as the interactive
shell. It intentionally does not install zsh, the JavaScript Node.js runtime, or project
dependencies. Pairing initializes only the Termroom Node identity and allowed roots; it does not
run project bootstrap commands. Add runtimes and dependencies in a derived image so the setup is
reproducible and can later map cleanly to a devcontainer-style execution environment:

```dockerfile
FROM ghcr.io/huhumanmaninganingansalamlam/termroom:latest
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends git nodejs npm \
    && rm -rf /var/lib/apt/lists/*
```

## Common commands

```bash
termroom serve .                    # Open the current project
termroom serve /path/to/project     # Open a specific project
termroom .                          # Backward-compatible shorthand
termroom attach .             # Attach directly to this Workspace's tmux session
termroom stop .               # Stop this Workspace's tmux session
termroom stop --core          # Stop only the Termroom web Core
```

The **Core** is Termroom's background web process. Normally one Core runs per computer
and manages multiple Workspaces.

When Docker or systemd owns the process lifecycle, run it in the foreground:

```bash
termroom /srv/projects --foreground --no-open
```

## Access from another device

The default bind address is `127.0.0.1`, so **only the computer running Termroom can
connect by default.**

For phones, tablets, or other computers, the recommended deployment is a private
VPN such as Tailscale. Bind Termroom only to the interface that needs it when possible;
`0.0.0.0` also exposes it to every reachable LAN interface.

To bind to other interfaces explicitly:

```bash
termroom ~/projects --host 0.0.0.0
```

Tailscale encrypts traffic between devices in the tailnet. A plain LAN does not:
when using an untrusted network, put Termroom behind an HTTPS reverse proxy and enable
secure cookies:

```bash
termroom ~/projects --host 0.0.0.0 --secure-cookie
```

**Do not expose Termroom directly to the public internet.** Password login is a
last application boundary, not a substitute for a firewall, private network, or HTTPS.
Use a strong unique password even inside a tailnet.

## Password and configuration

Login requires `TERMROOM_PASSWORD`. For a normal install, keep it in Termroom's global
configuration file at `~/.config/termroom/.env`.

```text
TERMROOM_PASSWORD=change-this-password
TERMROOM_LOCALE=en
```

`TERMROOM_LOCALE` controls the initial UI language for browsers that have not
chosen a language yet. Supported values are `en` and `ko`. A language selected
in the web UI is stored in that browser and overrides this default.

Local Workspaces are enabled by default. For a Docker Core that should only open
SSH or Termroom Node Workspaces, add this single setting to `.env`:

```text
TERMROOM_ALLOW_LOCAL_WORKSPACES=false
```

This removes local folders and Workspaces from the UI and makes their browse,
create, open, file, and terminal routes return `404`. It does not affect SSH or
Termroom Node Workspaces. The `./workspaces:/workspaces` bind mount can be removed
from `compose.yaml` in this mode.

Restrict the file so other users cannot read it:

```bash
chmod 600 ~/.config/termroom/.env
```

If a shell or service manager provides `TERMROOM_PASSWORD`, the environment variable takes
priority over the global `.env`. Project-folder `.env` files remain a compatibility fallback,
but the global file is recommended so Termroom's login password is not mixed with project
configuration.

Termroom does not enforce a minimum password length by default. Operators can opt in:

```bash
TERMROOM_MIN_PASSWORD_LENGTH=12
```

Persistent state defaults to `~/.config/termroom/`:

```text
.env                 # optional global password / locale / Workspace policy
termroom.sqlite3
access-token
credential-key
credentials/
ssh/
```

Override the location with `--config-dir` or `TERMROOM_CONFIG_DIR`.

SSH passwords are not stored as plaintext in project files or the SQLite DB. They are
stored in owner-only encrypted credential files under the config directory. This is not
a replacement for a hardware-backed vault because the local encryption key is stored in
the same owner-only config directory. The Termroom login password itself remains plaintext
in `.env`; encrypting it with a key beside it would not add a meaningful security boundary.
Termroom checks that a password-bearing `.env` is not accessible by group or other users,
and removes `TERMROOM_PASSWORD` from the Core process environment after loading it.

## Docker Compose

Versioned release images are published to GHCR after the matching PyPI release succeeds. The
image itself installs that exact `termroom` version from PyPI, so the Python package and
container release stay aligned. Docker-only fixes may update `latest` from `main` without
overwriting immutable version tags or publishing a new Python package.

To run the published image:

```bash
cp .env.example .env
# For the Core, keep TERMROOM_MODE=core and change TERMROOM_PASSWORD
docker compose pull
docker compose up -d --remove-orphans
```

The image is `ghcr.io/huhumanmaninganingansalamlam/termroom:latest`. Versioned tags
such as `0.1.1` and `0.1` are published as well. To build the container locally from
the PyPI package instead, use `docker compose up -d --build`; the Dockerfile accepts
`TERMROOM_VERSION` as a build argument.

`TERMROOM_MODE=core` in the shared `.env` selects the Core service;
`TERMROOM_MODE=node` selects the outbound-only Node service. The included Compose configuration
uses:

- `termroom-config:/config` for persistent DB, SSH-key, and credential state
- `${TERMROOM_WORKSPACES_HOST_PATH}:/workspaces` for the selected host project folder
- `${TERMROOM_BIND_HOST}:8765:8765` as the Core publish, defaulting to loopback

For direct Core-to-Node communication inside a tailnet, set `TERMROOM_BIND_HOST` to the Core
host's specific Tailscale IP and pair the Node with `http://<tailscale-ip>:8765`. This does not
open a Node port. Keep the default loopback bind when using Tailscale Serve or an HTTPS reverse
proxy. Avoid `0.0.0.0` unless host firewall rules already limit who can reach the port.

For an HTTPS reverse proxy, leave `TERMROOM_BIND_HOST=127.0.0.1` and set
`TERMROOM_SECURE_COOKIE=true`. Caddy, Nginx, and similar proxies must preserve the original Host
header and proxy WebSocket upgrades; their standard reverse-proxy configuration normally does
both. Native HTTP remains behind the proxy while the browser and HTTPS/WSS Nodes use the proxy
URL.

Termroom itself emits Content Security Policy, Permissions Policy, frame, content-type, and
referrer headers, and compresses eligible dynamic text responses when the client accepts gzip.
Let the proxy forward `Content-Encoding` and `Vary` unchanged. Do not add a second copy of
`Content-Security-Policy`, `Permissions-Policy`, `X-Frame-Options`, `X-Content-Type-Options`, or
`Referrer-Policy` for this host; disable those shared proxy directives or deliberately replace
the upstream value in one place. The application policy deliberately supports both direct local
HTTP and HTTPS/WSS reverse-proxy operation and does not force an insecure local URL to upgrade
itself. The Permissions Policy disables camera, microphone, geolocation, payment, and USB access;
it does not block the clipboard or fullscreen APIs used by normal browser workspace flows.

For an SSH/Node-only Core, keep `TERMROOM_MODE=core` and set
`TERMROOM_ALLOW_LOCAL_WORKSPACES=false`. That Core policy is separate from
`TERMROOM_MODE=node`, which runs this container as a Node process.

## PWA and languages

Termroom includes a PWA manifest and icons. On a secure installation, open **Settings →
Install Termroom** to use the browser's install prompt. On iPhone or iPad Safari, Settings
shows the **Share → Add to Home Screen** path instead. The install action stays small and is
hidden when Termroom is already running as an installed app.

PWA installation requires HTTPS or a browser-recognized loopback secure context. The Service
Worker does not offline-cache authenticated Workspace, file, terminal, or Run responses.

The UI defaults to English. Korean is available from the language selector. Locale
sources live in `termroom/locales/`.

## Technical overview

`tmux`, not the browser, is what keeps terminal work alive.

```text
Browser / PWA
      │
      ▼
Termroom Core
  ├─ Local filesystem + local tmux
  ├─ SFTP + OpenSSH + remote tmux
  └─ Termroom Node outbound WSS + remote filesystem/tmux
```

Local terminals connect xterm.js in the browser to a real PTY over WebSocket. SSH
Workspaces use the local OpenSSH client and SFTP. Node Workspaces use a paired,
capability-gated Node connection with local allowed-root policy. See
[`docs/architecture.md`](docs/architecture.md) for the data model and security boundaries.

## Development

For source development, use the project virtual environment:

```bash
git clone https://github.com/huhumanmaninganingansalamlam/termroom.git
cd termroom
uv sync --all-groups
```

Validation commands:

```bash
uv run --frozen ruff check termroom tests
uv run --frozen pytest
node --check termroom/static/app.js
node --check termroom/static/remote_run.js
node --check termroom/static/terminal.js
docker compose config
```

User-facing changes should also be exercised in real browsers across mobile, tablet,
and desktop layouts.

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — data model, terminal/file pipelines,
  and security boundaries
- [`docs/mobile-terminal.md`](docs/mobile-terminal.md) — mobile terminal and IME input
  contract
- [`docs/i18n.md`](docs/i18n.md) — locale and translation rules
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — development setup and contribution rules

## License

Termroom's original code is distributed under the [MIT License](LICENSE).
Vendored `@xterm/xterm 6.0.0` is also MIT-licensed; its upstream copyright notice is
preserved in [`termroom/static/vendor/xterm.LICENSE`](termroom/static/vendor/xterm.LICENSE).
