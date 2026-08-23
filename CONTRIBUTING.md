# Contributing to Termroom

Thanks for helping improve Termroom. Keep changes aligned with its narrow product
contract: a self-hosted workspace for persistent terminals, files, managed runs,
and quick resume across Linux Local/Node computers and Linux or macOS SSH computers.

## Setup

```bash
uv sync --all-groups
uv run termroom-assets
cp .env.example .env
# set TERMROOM_PASSWORD
chmod 600 .env
uv run termroom .
```

## Required checks

```bash
uv run --frozen ruff check termroom tests
uv run --frozen pytest
node --check termroom/static/app.js
node --check termroom/static/remote_run.js
node --check termroom/static/terminal.js
```

## Product rules

Prefer changes that improve at least one of these:

1. faster return to a project;
2. easier observation/interruption/resume of running work;
3. easier recovery of result files;
4. a consistent Local/SSH/Node mental model.

Avoid turning Termroom into a full IDE, server administration dashboard, built-in
AI agent, public relay, or remote desktop.

## UI/UX review

For user-facing changes, test the complete flow rather than one component:

```text
Home → Open workspace → Computer → Workspace
→ Terminal / Files / Recent → Home → Resume
```

Check at least 390px, 768px, and 1440px widths. Look for horizontal overflow,
truncated primary actions, touch targets below 44px on mobile, keyboard overlap,
focus states, empty/error/loading states, and accidental divergence between Local,
SSH, and Node screens.

Top-level navigation is for changing workspace sections. Actions for the current
view belong in that view's toolbar. Avoid duplicating the same action in several
unrelated surfaces.

## Files

Preserve workspace boundary checks, symlink protections, internal config hiding,
upload race protection, and bounded previews. Multi-select operations must work
for Local, SSH, and Node backends or clearly fail before making partial changes.

## Terminal

tmux is the persistence source of truth. Do not replace it with browser state.
Do not put `TERMROOM_PASSWORD` into spawned shell/tmux environments. Preserve
xterm IME/composition and bracketed-paste paths.

## SSH

Keep host-key pinning and credential storage intact. The normal managed-key path
should reuse one Ed25519 key. Existing-key mode is advanced functionality. Remote
Linux or macOS computers connected through SSH must not require a Termroom-specific
agent. Resolve commands through the shared login-shell bootstrap and verify them with
`command -v`; do not add guessed package-manager or executable directories. Keep filesystem
scans on the portable SFTP path unless a remote command is explicitly proven on both
GNU/Linux and macOS/BSD userlands.

## Node

Node is the outbound-only connection method for a Remote Linux computer, not a
separate product. Preserve one-time Pairing, fingerprint approval, Node-local
allowed roots and run root, owner-only identity state, capability gating, bounded
streaming, request idempotency, and reconnect behavior. Do not add a generic hidden
shell or let the Core expand Node-local filesystem policy.

## Managed runs and Activity

File Run uses the server-owned Runner Registry and one exact managed slot per
Workspace. Remote Run uses an explicit Bash command in a managed transient Workspace.
Do not infer runtimes or project commands, auto-install environments, or treat a
connection loss as proof of completion. State transitions and Activity Events must
remain idempotent across polling, multiple browser tabs, and Core/Remote reconnects.

## Localization

UI strings live in `termroom/locales/`. Korean and English files must retain identical key sets.
Do not hard-code Korean or English UI copy into templates or browser JavaScript.
