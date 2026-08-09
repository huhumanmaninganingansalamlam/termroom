# Termroom

[English](https://github.com/huhumanmaninganingansalamlam/termroom/blob/main/README.md) · [한국어](https://github.com/huhumanmaninganingansalamlam/termroom/blob/main/README.ko.md)

**A personal workspace for continuing terminal work from a browser.**

Run `termroom .` inside a project folder to open its terminals and files in your
browser. `tmux` keeps terminal processes alive, so closing the browser or switching
devices does not end the work that is already running.

Termroom is aimed at workflows such as starting a long build or AI task on a laptop,
checking it from a phone later, and working with remote Linux servers through the same
terminal-and-files interface.

> **Status: early release.** Termroom is available as a Python CLI package and the
> project is still evolving quickly. Expect small workflow and UI changes between
> early versions.

## When is it useful?

- Check a long-running command from a phone or tablet after starting it on your PC.
- Keep terminal work alive even when the browser is closed.
- Browse, upload, download, or edit project files from the browser.
- Move between several local projects from one interface.
- Use SSH Linux servers in a workflow similar to local projects.

Termroom is not a cloud IDE. **The real terminal processes and files stay on your Linux
computer or on an SSH server you control.** Termroom provides the browser interface.

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
printf '%s\n' 'TERMROOM_PASSWORD=choose-a-password' 'TERMROOM_LOCALE=en' > ~/.config/termroom/.env
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
- Preview images/PDFs, JSON/CSV, and bounded portions of large text files.
- Use the same Files UI for local and SSH projects.

### Remote Run

- Copy a Local/SSH Workspace folder, a public HTTPS Git repository, or one ZIP to a
  registered SSH server's temporary space.
- Run one command with that SSH user's installed tools and CPU/GPU/RAM.
- Keep the command alive in a dedicated remote `tmux` session when the browser disconnects.
- Open the prepared folder through the normal Workspace Terminal and Files UI; there is no
  separate log dashboard or file viewer.
- Keep completed files for 24 hours, with immediate deletion available from the temporary
  Workspace header.
- Remote Run does not configure environments, provide a sandbox, schedule jobs, or write
  changes back to the Source.

### Recent

- See recently created or modified files.
- See recently used terminals and activity timestamps.
- Identify files that are still growing.
- Exclude dependency/cache/hidden directories by default.
- Add project-specific rules with `.termroomignore`.

## The basic model

In Termroom, a **Workspace is simply one project folder**.

```text
Computer
└─ Workspace (project folder)
   ├─ Terminal
   ├─ Files
   └─ Recent
```

A `Computer` is either this Linux machine or an SSH Linux server you registered.

**Remote Run is a temporary Workspace shell, not a persistent project.** Its Source is copied
to an SSH server, its Terminal and Files reuse the normal Workspace UI, and its managed folder
is disposable. It does not appear in Recent Workspaces.

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

## Common commands

```bash
termroom .                    # Open the current project
termroom /path/to/project     # Open a specific project
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

For phones, tablets, or other computers, prefer an existing LAN, VPN/Tailscale network,
or an HTTPS reverse proxy that you operate.

To bind to other interfaces explicitly:

```bash
termroom ~/projects --host 0.0.0.0
```

Behind an HTTPS reverse proxy, enable secure cookies:

```bash
termroom ~/projects --host 0.0.0.0 --secure-cookie
```

Termroom is not designed to be exposed directly to the public internet without an
appropriate private network or HTTPS setup.

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
.env                 # optional global password / default locale
termroom.sqlite3
access-token
credential-key
credentials/
ssh/
```

Override the location with `--config-dir` or `TERMROOM_CONFIG_DIR`.

SSH passwords are not stored as plaintext in project files or the SQLite DB. They are
stored in owner-only encrypted credential files under the config directory. This is not
a replacement for a hardware-backed vault.

## Docker Compose

Release images are published to GHCR after the matching PyPI release succeeds. The
image itself installs that exact `termroom` version from PyPI, so the Python package
and container release stay aligned.

To run the published image:

```bash
cp .env.example .env
# Change TERMROOM_PASSWORD
docker compose pull
docker compose up -d
```

The image is `ghcr.io/huhumanmaninganingansalamlam/termroom:latest`. Versioned tags
such as `0.1.1` and `0.1` are published as well. To build the container locally from
the PyPI package instead, use `docker compose up -d --build`; the Dockerfile accepts
`TERMROOM_VERSION` as a build argument.

The included Compose configuration uses:

- `termroom-config:/config` for persistent DB, SSH-key, and credential state
- `./workspaces:/workspaces` for local content visible to the Core
- `127.0.0.1:8765:8765` as the default host publish

Replace the volume or bind mount to fit your deployment.

## PWA and languages

Termroom includes an installable PWA manifest and icons. The Service Worker does not
offline-cache authenticated Workspace, file, or terminal responses.

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
  └─ SFTP + OpenSSH + remote tmux
```

Local terminals connect xterm.js in the browser to a real PTY over WebSocket. SSH
Workspaces use the local OpenSSH client and SFTP. See
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
