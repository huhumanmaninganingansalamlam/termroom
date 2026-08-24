from __future__ import annotations

from pathlib import Path

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


def test_workspace_run_menu_asset_versions_are_bumped() -> None:
    base = (ROOT / "termroom/templates/base.html").read_text(encoding="utf-8")

    assert "app.css') }}?v=56" in base
    assert "app.js') }}?v=68" in base


def test_workspace_command_editing_stays_inside_each_command_card() -> None:
    template = (ROOT / "termroom/templates/workspace_base.html").read_text(
        encoding="utf-8"
    )

    assert 'class="workspace-run-command-card' in template
    assert 'class="workspace-run-command-editor"' in template
    assert 'class="workspace-run-command-edit-action"' in template
    assert 'class="workspace-run-command-launch"' in template
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
    assert "{% if not item and index > 0 %}hidden{% endif %}" in template
    assert "workspace_commands | length == 0 %}open" in template
    assert "data-workspace-run-command-add" in template
    assert "{{ t('workspace.run.add') }}" in template

    behavior_start = script.index(
        'const addCommand = menu.querySelector("[data-workspace-run-command-add]")'
    )
    sync_call = "    syncAddCommand();"
    behavior_end = script.index(sync_call, behavior_start) + len(sync_call)
    behavior = script[behavior_start:behavior_end]
    for contract in (
        'querySelectorAll("[data-workspace-run-command-card]")',
        "commandCards.some((card) => card.hidden)",
        "commandCards.find((candidate) => candidate.hidden)",
        "card.hidden = false;",
        "editor.open = true;",
        'input[name="commands"]',
        "focus({ preventScroll: true })",
    ):
        assert contract in behavior

    assert ".workspace-run-command-add {" in stylesheet
    assert ".workspace-run-command-add[hidden]" in stylesheet
    assert "border: 1px dashed var(--border);" in stylesheet
