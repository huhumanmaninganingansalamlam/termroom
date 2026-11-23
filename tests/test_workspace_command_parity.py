"""Exact Local/SSH/Node quick-command identity policy; transports stay distinct."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from termroom.terminals import classify_workspace_command_reuse

ROOT = Path(__file__).resolve().parents[1]

# Backend error envelopes intentionally differ: Local TerminalError,
# SSHBackendError, NodeAgentError(code=idempotency_conflict).
BACKENDS = ("local", "ssh", "node")
SCENARIOS = (
    ("same_launch_same_digest", "same", "same", "running", False, "replay"),
    (
        "same_launch_different_digest", "same", "different", "running", False,
        "idempotency_conflict",
    ),
    (
        "new_launch_changed_command_active", "other", "different", "running", False,
        "detach_changed",
    ),
    (
        "new_launch_changed_command_dead", "other", "different", "dead", True,
        "detach_changed",
    ),
    (
        "new_launch_identical_command_running", "other", "same", "running", False,
        "reuse_running",
    ),
    (
        "new_launch_identical_command_settling", "other", "same", "settling", False,
        "reuse_running",
    ),
    (
        "new_launch_identical_command_dead", "other", "same", "dead", True,
        "remove_dead",
    ),
    (
        "new_launch_identical_command_shell", "other", "same", "shell", False,
        "respawn_shell",
    ),
)


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(
    "scenario, record_launch, record_digest, state, dead, expected",
    SCENARIOS,
    ids=[item[0] for item in SCENARIOS],
)
def test_shared_semantic_decisions_not_backend_transport(
    backend: str, scenario: str, record_launch: str, record_digest: str,
    state: str, dead: bool, expected: str,
) -> None:
    # This matrix exercises pure decisions only. Real OS I/O is asserted in
    # test_workspace_commands.py and test_node_e2e.py, not faked here.
    decision = classify_workspace_command_reuse(
        {
            "launch_id": "current-launch" if record_launch == "same" else "other-launch",
            "digest": "abcd" if record_digest == "same" else "ef01",
            "state": state,
            "dead": dead,
        },
        launch_id="current-launch",
        digest="abcd",
    )
    assert decision == expected, (backend, scenario)


@pytest.mark.parametrize(
    "backend, source, class_name",
    (
        ("local", "termroom/terminals.py", "TerminalManager"),
        ("ssh", "termroom/ssh_backend.py", "SSHBackend"),
        ("node", "termroom/node_agent.py", "NodeRuntime"),
    ),
)
def test_concrete_backend_uses_shared_decision_but_owns_io(
    backend: str, source: str, class_name: str,
) -> None:
    tree = ast.parse((ROOT / source).read_text(encoding="utf-8"))
    klass = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    method = next(
        node for node in klass.body if isinstance(node, ast.FunctionDef)
        and node.name == "_run_workspace_command_locked"
    )
    calls = [
        node for node in ast.walk(method) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "classify_workspace_command_reuse"
    ]
    assert len(calls) == 1, (
        f"{backend} must call one common identity classifier, not duplicate policy"
    )
    errors = {
        backend: exception
        for backend, exception in (
            ("local", "TerminalError"),
            ("ssh", "SSHBackendError"),
            ("node", "NodeAgentError"),
        )
    }
    raised = [
        node for node in ast.walk(method)
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
        and node.exc.func.id == errors[backend]
    ]
    assert raised, f"{backend} lost its own transport failure semantics"
