# Working on Termroom

## Where to start

Termroom is a **human-first self-hosted Terminal + Files workspace**. The Linux
Core, SSH remote and outbound Linux Node reuse the user's real filesystem and
tmux sessions. It is not a cloud IDE, inferred agent-state dashboard, command
detector or managed development environment. Start with
[architecture](docs/architecture.md), [mobile Terminal](docs/mobile-terminal.md)
and [locales](docs/i18n.md); read the relevant implementation before editing.

## Feature map

| Change | Concrete owner | Direct tests |
| --- | --- | --- |
| Terminal, PTY, resize, WebSocket and input control | [terminal.js](termroom/static/terminal.js), [terminals.py](termroom/terminals.py), [terminal_control.py](termroom/terminal_control.py) | [terminal input](tests/test_terminal_input_ownership.py), [real tmux/browser](tests/test_tmux_multi_device.py), [WebSocket](tests/test_websocket.py) |
| TUI selection, copy and reading history | [selection](termroom/static/terminal_selection.js), [scrollback](termroom/static/mobile_scrollback.js), [copy view](termroom/templates/terminal.html) | [copy](tests/test_terminal_copy.py), [scrollback](tests/test_mobile_scrollback.py), [real Chrome](tests/test_tmux_multi_device.py) |
| Browser Workspace, Files and Quick Commands | [app.js](termroom/static/app.js), [templates](termroom/templates/workspace_base.html), [app routes](termroom/app.py), [files](termroom/files.py) | [Files/CAS](tests/test_files.py), [app](tests/test_app.py), [quick-command UI](tests/test_workspace_run_menu_contract.py) |
| Scoped non-UI client credentials and exact-action admission | [app routes](termroom/app.py), [auth](termroom/auth.py), [state](termroom/db.py), [control](termroom/terminal_control.py) | [auth](tests/test_auth.py), [control](tests/test_terminal_control.py), [real Chrome A/B/C](tests/test_tmux_multi_device.py) |
| Local and SSH Terminal/Workspace behavior | [local](termroom/terminals.py), [SSH](termroom/ssh_backend.py), [remote dispatch](termroom/remote_access.py) | [Local](tests/test_terminals.py), [SSH](tests/test_ssh_backend.py), [parity/browser](tests/test_tmux_multi_device.py) |
| Node pairing, protocol, remote Terminal and reconnect | [Node Core](termroom/node_core.py), [Node runtime](termroom/node_agent.py), [protocol](termroom/node_protocol.py) | [Node real E2E](tests/test_node_e2e.py), [protocol](tests/test_node_protocol.py) |
| File Run and explicit Remote Run, result ZIP and guarded import | [file_runs.py](termroom/file_runs.py), [remote_runs.py](termroom/remote_runs.py), [run_results.py](termroom/run_results.py) | [File Run](tests/test_file_runs.py), [Remote Run](tests/test_remote_run_lifecycle.py), [Node real E2E](tests/test_node_e2e.py) |
| Browser assets, locale parity, product packaging | [assets.py](termroom/assets.py), [i18n.py](termroom/i18n.py), [CI](.github/workflows/ci.yml) | [assets](tests/test_assets.py), [i18n](tests/test_i18n.py), [CLI](tests/test_cli.py) |

## Checks

Prerequisites: Linux, Python 3.12+, uv, node, tmux; integration tests
also require real Google Chrome and, for SSH, ssh/sshd and ssh-keygen.
Tests use **private tmux sockets and disposable Chrome profiles**, never your
running terminal sessions. Work inside the product Git root (repos/ in the
private task workspace).

    uv sync --all-groups --frozen
    uv run --frozen ruff check termroom tests
    uv run --frozen pytest -q tests/test_terminal_copy.py tests/test_mobile_scrollback.py
    uv run --frozen pytest -q
    uv run --frozen pytest -q tests/test_websocket.py  # separate process in CI
    node --check termroom/static/terminal.js
    node --check termroom/static/mobile_scrollback.js

See [CONTRIBUTING](CONTRIBUTING.md) for setup and
[CI](.github/workflows/ci.yml) for the source-of-truth complete checks.

## Safety boundaries

- A PTY write or command acknowledgement **is not process completion**.
  Keep accepted, observed, refused and unknown results distinct.
- Files use root/path confinement and **CAS digest + mtime**; never overwrite
  newer content after a conflict. No silently generated commands.
- Control API actions use exact Workspace/Terminal/pane identities,
  idempotency receipts and revocation checks. Never widen scope or swallow
  terminal-control ownership failures.
- Local, SSH and Node share policy semantics, **not identical transports**:
  keep SSH host-key verification, Node capabilities and individual tmux
  operations concrete. Do not introduce a BaseBackend or generic action bus.
- Never log raw credentials, tokens, SSH keys or private command content.
- A changed browser interaction requires a real UI/PTY regression when
  possible. Browser emulation is **not** physical-device acceptance; identify
  missing Safari/touch/remote-host evidence explicitly.
- Work in one domain slice at a time; **do not weaken assertions**, hide
  failing checks, upgrade vendored xterm opportunistically or treat a passing
  local test as deployment acceptance.

## Task ownership

Canonical task records and the work board live in the **parent private
workspace**, under ../docs/tasks/, operated with ../scripts/repoctl from
the parent root. They are not GitHub/Gitea issue forms, and this repository
does not need a second task-template system. Source commits, Task completion,
release publication and actual deployment are separate evidence states.
Historical prototype fixtures in
[terminal-first design review](docs/prototypes/terminal-first/README.md)
are not running product code.
