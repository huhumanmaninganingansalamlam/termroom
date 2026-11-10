from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from termroom.app import create_app
from termroom.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_workspace_run_menu_has_an_accessible_close_control() -> None:
    template = (ROOT / "termroom/templates/workspace_base.html").read_text(encoding="utf-8")
    stylesheet = (ROOT / "termroom/static/app.css").read_text(encoding="utf-8")

    assert "data-workspace-run-close" in template
    assert "aria-label=\"{{ t('common.close') }}\"" in template
    assert "title=\"{{ t('common.close') }}\"" in template
    assert ".workspace-run-close {" in stylesheet
    assert "width: 44px;" in stylesheet
    assert "height: 44px;" in stylesheet
    assert ".workspace-run-close:focus-visible" in stylesheet
    close_icon = stylesheet[stylesheet.index(".workspace-run-close svg {") :]
    assert "display: block;" in close_icon.split("}", 1)[0]


def test_workspace_run_menu_dismissal_preserves_terminal_focus_contract() -> None:
    script = (ROOT / "termroom/static/app.js").read_text(encoding="utf-8")
    start = script.index("  const workspaceRunMenus = [")
    end = script.index("  const workspaceCommandForms = [", start)
    behavior = script[start:end]

    for contract in (
        'querySelectorAll("[data-workspace-run-menu]")',
        'querySelector("[data-workspace-run-close]")',
        "if (!(menu instanceof HTMLDetailsElement) || !menu.open) return false;",
        'menu.querySelector("summary")?.focus({ preventScroll: true });',
        "menu.open && !menu.contains(event.target)",
        'if (event.key !== "Escape") return;',
        "closeWorkspaceRunMenu(menu, { restoreFocus: true });",
    ):
        assert contract in behavior

    outside_click = behavior.index("menu.open && !menu.contains(event.target)")
    outside_close = behavior.index(".forEach((menu) => closeWorkspaceRunMenu(menu));")
    assert outside_click < outside_close
    assert "restoreFocus: true" not in behavior[outside_click:outside_close]


def test_workspace_command_editing_stays_inside_each_command_card() -> None:
    template = (ROOT / "termroom/templates/workspace_base.html").read_text(
        encoding="utf-8"
    )

    assert 'class="workspace-run-command-card' in template
    assert 'class="workspace-run-command-editor"' in template
    assert 'class="workspace-run-command-edit-action"' in template
    assert 'class="workspace-run-command-launch"' in template
    assert "data-workspace-command-edit-form" in template
    assert 'name="command"' in template
    assert "/run-commands/{{ item.slot }}/save" in template
    assert "/run-commands/add" in template
    assert "data-workspace-run-command-cancel" in template
    assert "data-workspace-run-command-save" in template
    assert "data-workspace-command-run-button" in template
    assert "workspace-run-command-config" not in template
    assert template.index('class="workspace-run-command-edit-action"') < template.index(
        'class="workspace-run-command-launch"'
    )
    assert 'class="workspace-run-config"' not in template

    stylesheet = (ROOT / "termroom/static/app.css").read_text(encoding="utf-8")
    heading = stylesheet[stylesheet.index(".workspace-run-heading {") :]
    assert "padding-right" not in heading.split("}", 1)[0]


def test_workspace_run_commands_reveal_progressively() -> None:
    template = (ROOT / "termroom/templates/workspace_base.html").read_text(encoding="utf-8")
    script = (ROOT / "termroom/static/app.js").read_text(encoding="utf-8")
    stylesheet = (ROOT / "termroom/static/app.css").read_text(encoding="utf-8")

    assert "data-workspace-run-command-list" in template
    assert "data-workspace-run-command-card" in template
    assert "data-workspace-run-command-persisted" in template
    assert "{% if not item and index > 0 %}hidden{% endif %}" in template
    assert "workspace_commands | length == 0 %}open" not in template
    assert "data-workspace-run-command-add" in template
    assert "{{ t('workspace.run.add') }}" in template
    assert "data-workspace-run-command-remove" in template
    assert "data-workspace-command-delete" in template
    assert "/run-commands/{{ item.slot }}/delete" in template
    assert "{{ t('workspace.run.delete') }}" in template

    behavior_start = script.index(
        'const addCommand = menu.querySelector("[data-workspace-run-command-add]")'
    )
    behavior_end = script.index("  document.addEventListener(\"click\"", behavior_start)
    behavior = script[behavior_start:behavior_end]
    for contract in (
        'querySelectorAll("[data-workspace-run-command-card]")',
        "persistedCommandCards",
        "emptyCommandCards",
        "commandCards.some((card) => card.hidden)",
        "commandCards.find((candidate) => candidate.hidden)",
        "card.hidden = false;",
        'card.querySelector("summary")?.focus({ preventScroll: true });',
        "compactEmptyCommandCards",
        "resetEmptyCommandCard",
        'querySelector("[data-workspace-run-command-remove]")',
        'input[name="command"]',
        "input.defaultValue.trim()",
        'querySelector("[data-workspace-run-command-save]")',
        'querySelector("[data-workspace-command-run-button]")',
        "run.disabled = dirty;",
        'querySelector("[data-workspace-run-command-cancel]")',
        "input.value = input.defaultValue;",
        "focus({ preventScroll: true })",
    ):
        assert contract in behavior
    assert "editor.open = true;" not in behavior
    assert "input[name=\"commands\"]')?.focus" not in behavior

    assert ".workspace-run-command-add {" in stylesheet
    assert ".workspace-run-command-add[hidden]" in stylesheet
    assert "border: 1px dashed var(--border);" in stylesheet
    assert ".workspace-run-command-delete {" in stylesheet
    assert ".workspace-run-command-cancel," in stylesheet
    assert ".workspace-run-command-save:disabled" in stylesheet
    assert ".workspace-run-command-launch button:disabled" in stylesheet
    assert ".workspace-run-command-actions {" in stylesheet
    fields = stylesheet[stylesheet.index(".workspace-run-command-fields {") :]
    assert "grid-template-columns: minmax(0, 1fr);" in fields.split("}", 1)[0]


def test_terminal_first_desktop_rail_keeps_existing_workspace_routes() -> None:
    shell = (ROOT / "termroom/templates/workspace_base.html").read_text(encoding="utf-8")
    terminal = (ROOT / "termroom/templates/terminal.html").read_text(encoding="utf-8")
    css = (ROOT / "termroom/static/app.css").read_text(encoding="utf-8")

    assert 'href="/w/{{ workspace.id }}/terminal"' in shell
    assert 'href="/w/{{ workspace.id }}/files"' in shell
    assert 'href="/w/{{ workspace.id }}/recent"' in shell
    assert "{{ workspace_run_menu('navigation') }}" in shell
    assert "data-terminal-switch=" in terminal
    assert "id=\"terminal\"" in terminal
    assert "terminal.js" in terminal

    # Only the Terminal desktop shell contracts; other routes and mobile stay in place.
    scoped = css.split("/* Terminal-first candidate A:", 1)[1]
    assert "@media (min-width: 1024px)" in scoped
    assert "  .terminal-page {\n    --sidebar-width: 56px;" in scoped
    assert ".terminal-page .bottom-nav > a svg" in scoped
    assert ".terminal-page .workspace-run-menu-navigation .workspace-run-popover" in scoped
    assert ".terminal-page .terminal-workspace-identity" in scoped
    assert "position: fixed;" in scoped


def test_terminal_first_rail_utility_actions_are_real_and_named() -> None:
    shell = (ROOT / "termroom/templates/workspace_base.html").read_text(encoding="utf-8")
    terminal = (ROOT / "termroom/templates/terminal.html").read_text(encoding="utf-8")
    css = (ROOT / "termroom/static/app.css").read_text(encoding="utf-8")

    assert 'class="terminal-rail-utilities" role="group"' in shell
    assert "{{ user_settings_menu('terminal-rail', workspace) }}" in shell
    assert 'data-summary-url="/api/activity/summary"' in shell
    assert 'aria-label="{{ t(\'activity.heading\') }}"' in shell
    assert 'class="terminal-workspace-identity"' in terminal
    assert '{{ workspace.display_name }}' in terminal
    assert '{{ t(workspace_kind_label) }}' in terminal
    assert ".terminal-page .terminal-rail-utilities .user-menu-popover" in css
    assert ".terminal-page .workspace-sidebar-usage-menu .workspace-usage-popover" in css
    assert "<iframe" not in shell
    assert "<iframe" not in terminal


def test_terminal_files_drawer_uses_authenticated_read_only_file_results() -> None:
    shell = (ROOT / "termroom/templates/workspace_base.html").read_text(encoding="utf-8")
    js = (ROOT / "termroom/static/app.js").read_text(encoding="utf-8")
    partial = (ROOT / "termroom/templates/_file_results.html").read_text(encoding="utf-8")

    assert "data-terminal-files-trigger" in shell
    assert '<dialog class="terminal-files-drawer"' in shell
    assert 'href="/w/{{ workspace.id }}/files"' in shell
    assert "data-terminal-files-list" in shell
    assert "data-terminal-files-close" in shell
    assert '"x-termroom-partial": "file-results"' in js
    assert "terminalFilesDrawer.showModal()" in js
    assert 'typeof terminalFilesDrawer.showModal === "function"' in js
    assert "terminalFilesDrawer.close()" in js
    assert "controller?.abort();" in js
    assert "retryButton.hidden = true;" in js
    assert "retryButton.hidden = false;" in js
    assert 'doc.querySelector(".file-search-summary strong")' in js
    assert 'const name = parent ? filename + " — " + parent : filename;' in js
    assert "target.origin !== window.location.origin" in js
    assert "document.createTextNode(name)" in js
    assert 'link.rel = "noopener";' in js
    assert "data-file-search-metadata" in partial
    assert "workspace-file-list" in partial
    assert "file-browser-row" in partial


@pytest.mark.asyncio
async def test_terminal_files_drawer_fetch_does_not_change_last_open_tab(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    project = root / "project"
    project.mkdir(parents=True)
    (project / "README.md").write_text("hello\n", encoding="utf-8")
    (project / "sub").mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    workspace = app.state.workspaces.open("project")
    app.state.terminals.ensure_workspace(workspace)
    wid = str(workspace["id"])
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = await client.post("/login", data={"password": "test-token"}, follow_redirects=False)
        assert auth.status_code == 303
        terminal = await client.get(f"/w/{wid}/terminal")
        assert terminal.status_code == 200
        assert "data-terminal-files-trigger" in terminal.text
        assert "data-terminal-files-drawer" in terminal.text
        assert app.state.store.get_workspace(wid)["last_tab"] == "terminal"

        partial = await client.get(
            f"/w/{wid}/files",
            params={"path": "."},
            headers={"x-termroom-partial": "file-results"},
        )
        assert partial.status_code == 200
        assert partial.headers["content-type"].startswith("text/html")
        assert "<html" not in partial.text
        assert "data-file-search-metadata" in partial.text
        assert "workspace-file-list" in partial.text
        assert f'href="/w/{wid}/view/README.md"' in partial.text
        assert f'href="/w/{wid}/files?path=sub"' in partial.text
        assert app.state.store.get_workspace(wid)["last_tab"] == "terminal"

        full_files = await client.get(f"/w/{wid}/files")
        assert full_files.status_code == 200
        assert app.state.store.get_workspace(wid)["last_tab"] == "files"


def test_terminal_files_drawer_touch_targets_are_large_enough() -> None:
    css = (ROOT / "termroom/static/app.css").read_text(encoding="utf-8")

    def rules(selector: str) -> str:
        return css.split(selector, 1)[1].split("}", 1)[0]

    close = rules(".terminal-files-drawer-header button {")
    assert "width: 44px;" in close
    assert "height: 44px;" in close
    search_and_footer = rules(
        ".terminal-files-drawer-search button,\n.terminal-files-drawer-footer > * {"
    )
    assert "min-height: 44px;" in search_and_footer
    assert "min-height: 44px;" in rules(".terminal-files-drawer-list a {")
    pagination = rules(".terminal-files-drawer-pagination a {")
    assert "min-height: 44px;" in pagination
    assert "min-width: 44px;" in pagination
    assert "display: inline-flex;" in pagination
