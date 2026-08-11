from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_source_switch_disables_only_inactive_panel_controls() -> None:
    script = (ROOT / "termroom/static/remote_run.js").read_text(encoding="utf-8")

    assert 'form.querySelectorAll("[data-source-panel]")' in script
    assert "panel.querySelectorAll(sourcePanelControlSelector)" in script
    assert "sourceControlInitialDisabled.set(control, control.disabled)" in script
    assert "control.disabled = !active || sourceControlInitialDisabled.get(control)" in script


def test_remote_run_pages_load_current_form_script() -> None:
    for template_name in ("remote_run_new.html", "remote_run_wait.html"):
        template = (ROOT / "termroom/templates" / template_name).read_text(encoding="utf-8")
        assert "remote_run.js') }}?v=10" in template

    workspace_template = (
        ROOT / "termroom/templates/workspace_base.html"
    ).read_text(encoding="utf-8")
    assert "data-remote-run-workspace" in workspace_template
    assert "data-run-retention" in workspace_template
    assert "remote_run.js') }}?v=10" in workspace_template

    home_template = (ROOT / "termroom/templates/home.html").read_text(encoding="utf-8")
    assert "/remote-runs/new" not in home_template
    assert "data-remote-run-recent" in home_template
    assert "run.command_summary" not in home_template
    assert "run.target_label" in home_template
    assert "run.created_label" in home_template
    assert "remote_run.js') }}?v=10" in home_template

    new_template = (ROOT / "termroom/templates/remote_run_new.html").read_text(
        encoding="utf-8"
    )
    assert "remote_run.exclusions_heading" in new_template
    assert "remote_run.exclusions_copy" in new_template


def test_mobile_remote_run_action_does_not_wrap() -> None:
    styles = (ROOT / "termroom/static/app.css").read_text(encoding="utf-8")

    assert ".remote-workspace-actions {\n  flex: 0 0 auto;" in styles
    assert ".remote-workspace-actions button {\n  white-space: nowrap;" in styles


def test_wait_page_surfaces_offline_status_and_clears_it_after_recovery() -> None:
    script = (ROOT / "termroom/static/remote_run.js").read_text(encoding="utf-8")
    template = (ROOT / "termroom/templates/remote_run_wait.html").read_text(
        encoding="utf-8"
    )

    assert 'wait.querySelector("[data-run-connection]")' in script
    assert 'connectionNotice.hidden = result.connection !== "offline"' in script
    assert "if (errorBox) errorBox.hidden = true" in script
    assert "data-run-connection hidden" in template
    assert "remote_run.connection_lost" in template
    assert "remote_run.connection_lost_copy" in template


def test_archive_retry_reuses_the_claimed_run_and_workspace_keeps_polling() -> None:
    script = (ROOT / "termroom/static/remote_run.js").read_text(encoding="utf-8")

    assert "pendingArchiveRun?.fingerprint === fingerprint" in script
    assert "pendingArchiveRun = { id: runId, fingerprint }" in script
    assert 'document.querySelector("[data-remote-run-workspace]")' in script
    assert '["preparing", "running"].includes(workspaceRun.dataset.state)' in script
    assert "4000 - (Date.now() - requestedAt)" in script
    assert 'document.querySelectorAll("[data-run-force-stop]")' in script

    workspace_template = (
        ROOT / "termroom/templates/workspace_base.html"
    ).read_text(encoding="utf-8")
    assert "data-run-force-stop" in workspace_template
    assert "/remote-runs/{{ remote_run.id }}/kill" in workspace_template

    wait_template = (
        ROOT / "termroom/templates/remote_run_wait.html"
    ).read_text(encoding="utf-8")
    assert "data-run-force-stop" in wait_template
    assert "/remote-runs/{{ run.id }}/kill" in wait_template


def test_active_home_runs_reconcile_without_blocking_server_render() -> None:
    script = (ROOT / "termroom/static/remote_run.js").read_text(encoding="utf-8")
    recent_templates = [
        (ROOT / "termroom/templates" / template_name).read_text(encoding="utf-8")
        for template_name in ("home.html", "workspace_open.html")
    ]

    assert 'document.querySelectorAll("[data-remote-run-recent]")' in script
    assert all('data-run-state="{{ run.state }}"' in template for template in recent_templates)
    assert (
        '.filter((row) => ["preparing", "running"].includes(row.dataset.runState))'
        in script
    )
    assert "Promise.allSettled(recentRuns.map" in script
    assert '!["preparing", "running"].includes(result.value.state)' in script
    assert "window.location.reload()" in script


def test_folder_browser_separates_navigation_from_commit_actions() -> None:
    template = (ROOT / "termroom/templates/workspace_open.html").read_text(
        encoding="utf-8"
    )
    stylesheet = (ROOT / "termroom/static/app.css").read_text(encoding="utf-8")

    assert 'class="folder-browser-navigation"' in template
    assert 'class="folder-browser-selection"' in template
    assert "project.new_here" in template
    assert template.index('class="folder-browser-list"') < template.index(
        'class="folder-browser-selection"'
    )
    assert ".remote-folder-browser .path-picker-header-actions" in stylesheet
    assert "display: contents" in stylesheet
    assert ".project-create-dialog[open]" in stylesheet
    assert "max-height: calc(100dvh - 12px)" in stylesheet
