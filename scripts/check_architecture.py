"""Executable checks for completed Termroom ownership decisions, not style quotas."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLD_CONTROL_SQL = {
    "_client_credential_row", "create_client_credential", "get_client_credential",
    "list_client_credentials", "touch_client_credential", "revoke_client_credential",
    "claim_client_action", "get_client_action", "update_client_action",
}
CONTROL_SQL = {
    "create_credential", "get_credential", "list_credentials",
    "touch_credential", "revoke_credential", "claim_action", "get_action",
    "advance_action",
}
BACKENDS = {
    "termroom/terminals.py": "TerminalManager",
    "termroom/ssh_backend.py": "SSHBackend",
    "termroom/node_agent.py": "NodeRuntime",
}


def find_class(source: str, name: str) -> ast.ClassDef | None:
    return next((node for node in ast.parse(source).body
                 if isinstance(node, ast.ClassDef) and node.name == name), None)


def check_app_routes(source: str) -> list[str]:
    tree = ast.parse(source)
    factory = next((node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "create_app"), None)
    if factory is None:
        return ["termroom/app.py: create_app composition function missing"]
    errors = []
    for handler in factory.body:
        if not isinstance(handler, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in handler.decorator_list:
            if not (isinstance(decorator, ast.Call) and decorator.args
                    and isinstance(decorator.args[0], ast.Constant)
                    and isinstance(decorator.args[0].value, str)
                    and isinstance(decorator.func, ast.Attribute)
                    and isinstance(decorator.func.value, ast.Name)
                    and decorator.func.value.id == "app"):
                continue
            path = decorator.args[0].value
            if path.startswith(("/api/control/v1", "/api/client-credentials")):
                errors.append(
                    f"termroom/app.py:{handler.lineno} {handler.name} {path}: "
                    "move route into termroom/control_api.py:register_control_routes"
                )
    return errors


def check_store(db_source: str, control_source: str) -> list[str]:
    state = find_class(db_source, "StateStore")
    control = find_class(control_source, "ControlStore")
    errors = []
    if state is None:
        return ["termroom/db.py: StateStore class missing"]
    for method in state.body:
        if isinstance(method, ast.FunctionDef) and method.name in OLD_CONTROL_SQL:
            errors.append(
                f"termroom/db.py:{method.lineno} StateStore.{method.name}: "
                "credential/action SQL belongs only in termroom/control_store.py"
            )
    if control is None:
        return errors + ["termroom/control_store.py: ControlStore class missing"]
    methods = {m.name for m in control.body if isinstance(m, ast.FunctionDef)}
    for missing in sorted(CONTROL_SQL - methods):
        errors.append(
            f"termroom/control_store.py: missing ControlStore.{missing}; "
            "restore concrete credential/action SQL owner"
        )
    return errors


def check_backend(source: str, path: str, class_name: str) -> list[str]:
    klass = find_class(source, class_name)
    if klass is None:
        return [f"{path}: {class_name} class missing"]
    method = next((m for m in klass.body if isinstance(m, ast.FunctionDef)
                   and m.name == "_run_workspace_command_locked"), None)
    if method is None:
        return [f"{path}: {class_name}._run_workspace_command_locked missing"]
    calls = sum(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "classify_workspace_command_reuse"
        for node in ast.walk(method)
    )
    if calls != 1:
        return [
            f"{path}:{method.lineno} {class_name}: expected one "
            "classify_workspace_command_reuse call; do not duplicate "
            "managed-command identity policy across transports"
        ]
    return []


def check_frontend(
    app: str, scrollback: str, base: str, workspace: str, terminal: str,
) -> list[str]:
    errors = []
    for token, owner in (
        ("data-workspace-run-menu", "workspace_run_menu.js"),
        ("data-terminal-files-drawer", "terminal_files_drawer.js"),
    ):
        if token in app:
            errors.append(
                f"termroom/static/app.js: {token} handler returned; "
                f"keep its single owner in termroom/static/{owner}"
            )
    if (
        "#terminal-copy-selection" in scrollback
        or "navigator.clipboard.writeText" in scrollback
    ):
        errors.append(
            "termroom/static/mobile_scrollback.js: copy UI/Clipboard API drift; "
            "use terminal_copy.js without a second selection state"
        )
    for script in (
        "terminal_selection.js", "mobile_scrollback.js", "terminal_copy.js",
        "terminal_files_drawer.js",
    ):
        if script in base:
            errors.append(
                f"termroom/templates/base.html: {script} loaded on all pages; "
                "load it only in terminal.html"
            )
        if f"static_asset_version('{script}')" not in terminal:
            errors.append(
                f"termroom/templates/terminal.html: hashed {script} missing"
            )
    if "static_asset_version('workspace_run_menu.js')" not in workspace:
        errors.append(
            "termroom/templates/workspace_base.html: "
            "missing hashed workspace_run_menu.js"
        )
    return errors


def check_chrome(source: str, path: str) -> list[str]:
    if "DevToolsActivePort" in source or "--remote-debugging-port=0" in source:
        return [
            f"{path}: duplicate Chrome CDP/profile lifecycle; "
            "use tests/support/chrome.py:open_chrome_page"
        ]
    return []


def inspect(root: Path = ROOT) -> list[str]:
    def get(path: str) -> str:
        return (root / path).read_text(encoding="utf-8")

    errors = check_app_routes(get("termroom/app.py"))
    errors += check_store(get("termroom/db.py"), get("termroom/control_store.py"))
    for path, name in BACKENDS.items():
        errors += check_backend(get(path), path, name)
    errors += check_frontend(
        get("termroom/static/app.js"),
        get("termroom/static/mobile_scrollback.js"),
        get("termroom/templates/base.html"),
        get("termroom/templates/workspace_base.html"),
        get("termroom/templates/terminal.html"),
    )
    for path in ("tests/test_node_e2e.py", "tests/test_tmux_multi_device.py"):
        errors += check_chrome(get(path), path)
    return errors


if __name__ == "__main__":
    failures = inspect()
    for failure in failures:
        print(f"ARCHITECTURE: {failure}", file=sys.stderr)
    if failures:
        sys.exit(1)
    print("Architecture ownership: accepted boundaries are intact")
