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

## Checks

```bash
uv run --frozen ruff check termroom tests
uv run --frozen pytest
node --check termroom/static/app.js
node --check termroom/static/remote_run.js
node --check termroom/static/terminal.js
```

## UI/UX review

```text
Home → Open workspace → Computer → Workspace
→ Terminal / Files / Recent → Home → Resume
```
