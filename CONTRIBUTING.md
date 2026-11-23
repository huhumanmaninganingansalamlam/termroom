# Contributing to Termroom

Thanks for helping improve Termroom. Keep changes aligned with its narrow product
contract: a self-hosted workspace for persistent terminals, files, managed runs,
and quick resume across Linux Local/Node computers and Linux or macOS SSH computers.

## Feature ownership

Use the [repository feature map and safety boundaries](AGENTS.md) to locate
the one product owner and its direct tests. Design and security details remain
in [architecture](docs/architecture.md); [mobile input/copy](docs/mobile-terminal.md)
and [i18n](docs/i18n.md) are separate feature contracts. Prototype fixtures
are historical review artifacts, not an implementation source.

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
