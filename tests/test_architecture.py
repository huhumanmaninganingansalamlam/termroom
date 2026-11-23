"""Negative fixtures prove anti-regression guardrails really reject ownership drift."""
from __future__ import annotations

from scripts.check_architecture import (
    check_app_routes,
    check_backend,
    check_chrome,
    check_frontend,
    check_store,
    inspect,
)


def test_current_boundaries_are_enforced_by_normal_pytest() -> None:
    assert inspect() == []


def test_control_route_cannot_move_back_into_app_factory() -> None:
    source = """
def create_app():
    @app.get("/api/control/v1/workspaces")
    async def unauthorized_control_route(): pass
"""
    errors = check_app_routes(source)
    assert len(errors) == 1
    assert "unauthorized_control_route" in errors[0]
    assert "termroom/app.py" in errors[0]
    assert "termroom/control_api.py" in errors[0]
    assert check_app_routes("def create_app():\n    return app\n") == []


def test_credential_sql_cannot_reappear_in_monolithic_state_store() -> None:
    errors = check_store(
        "class StateStore:\n    def get_client_action(self): pass\n",
        "class ControlStore:\n    pass\n",
    )
    assert any("StateStore.get_client_action" in error for error in errors)
    assert any("ControlStore.claim_action" in error for error in errors)
    assert all("control_store.py" in error for error in errors)


def test_each_backend_must_call_pure_identity_classifier_once() -> None:
    source = """
class SSHBackend:
    def _run_workspace_command_locked(self):
        return existing["launch_id"] == launch_id
"""
    errors = check_backend(source, "termroom/ssh_backend.py", "SSHBackend")
    assert len(errors) == 1
    assert "classify_workspace_command_reuse" in errors[0]
    assert "ssh_backend.py" in errors[0]
    valid = """
class SSHBackend:
    def _run_workspace_command_locked(self):
        return classify_workspace_command_reuse(existing, launch_id=x, digest=y)
"""
    assert check_backend(valid, "termroom/ssh_backend.py", "SSHBackend") == []


def test_frontend_cannot_reintroduce_global_copy_or_drawer_handlers() -> None:
    script = """
document.querySelector('[data-workspace-run-menu]')
document.querySelector('[data-terminal-files-drawer]')
"""
    errors = check_frontend(
        script,
        'document.querySelector("#terminal-copy-selection")',
        "<script src='mobile_scrollback.js'></script>",
        "<script src='workspace_run_menu.js?v={{ "
        "static_asset_version('workspace_run_menu.js') }}'>",
        "terminal_copy.js",
    )
    assert any("app.js" in error and "terminal_files_drawer.js" in error
               for error in errors)
    assert any("app.js" in error and "workspace_run_menu.js" in error
               for error in errors)
    assert any("mobile_scrollback.js" in error and "terminal_copy.js" in error
               for error in errors)
    assert any("base.html" in error for error in errors)
    assert any("terminal.html" in error for error in errors)


def test_real_browser_harness_must_not_clone_chrome_profile() -> None:
    errors = check_chrome(
        'port_file = profile / "DevToolsActivePort"',
        "tests/test_node_e2e.py",
    )
    assert len(errors) == 1
    assert "tests/support/chrome.py" in errors[0]
    assert check_chrome("with open_chrome_page(profile) as browser: pass",
                        "tests/test_node_e2e.py") == []
