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

    assert "app.css') }}?v=42" in base
    assert "app.js') }}?v=61" in base
