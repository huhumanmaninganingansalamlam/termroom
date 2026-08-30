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

    assert "app.css') }}?v=62" in base
    assert "app.js') }}?v=71" in base


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
