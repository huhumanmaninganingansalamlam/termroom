from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

_REAL_SUBPROCESS_RUN = subprocess.run
_REAL_SUBPROCESS_POPEN = subprocess.Popen


class _IsolatedTmuxPopen(_REAL_SUBPROCESS_POPEN[Any]):
    def __init__(self, args: Any, *pargs: Any, **kwargs: Any) -> None:
        environment = _isolated_tmux_environment(kwargs.get("env"))
        kwargs["env"] = environment
        super().__init__(
            _isolated_tmux_command(args, environment=environment),
            *pargs,
            **kwargs,
        )


def _tmux_global_socket(args: list[str] | tuple[str, ...]) -> tuple[str, str] | None:
    """Return an explicit global tmux socket option, ignoring command flags."""

    index = 1
    while index < len(args):
        argument = str(args[index])
        if argument == "--":
            break
        if not argument.startswith("-") or argument == "-":
            break
        if argument in {"-S", "-L"}:
            value = str(args[index + 1]) if index + 1 < len(args) else ""
            return argument, value
        if argument.startswith("-S") or argument.startswith("-L"):
            return argument[:2], argument[2:]
        if argument in {"-c", "-f", "-T"}:
            index += 2
            continue
        index += 1
    return None


def _isolated_tmux_command(args: Any, *, environment: dict[str, str]) -> Any:
    if not (
        isinstance(args, (list, tuple))
        and args
        and Path(str(args[0])).name == "tmux"
    ):
        return args
    socket_path = environment.get("PYTEST_TMUX_SOCKET", "")
    real_tmux = environment.get("PYTEST_REAL_TMUX", "")
    if not socket_path or not real_tmux:
        raise RuntimeError("pytest refused a tmux command without its private socket")
    explicit = _tmux_global_socket(args)
    if explicit is not None:
        option, value = explicit
        if option != "-S" or value != socket_path:
            raise RuntimeError("pytest refused a tmux command targeting another server")
        return args
    return [real_tmux, "-S", socket_path, *args[1:]]


def _prepend_path(directory: str, current: str) -> str:
    entries = [entry for entry in current.split(os.pathsep) if entry]
    return os.pathsep.join([directory, *[entry for entry in entries if entry != directory]])


def _isolated_tmux_environment(environment: Any = None) -> dict[str, str]:
    isolated = dict(os.environ if environment is None else environment)
    for name in (
        "PYTEST_TMUX_SOCKET",
        "PYTEST_REAL_TMUX",
        "PYTEST_TMUX_WRAPPER_DIR",
    ):
        value = os.environ.get(name)
        if value:
            isolated[name] = value
    wrapper_dir = isolated.get("PYTEST_TMUX_WRAPPER_DIR", "")
    if wrapper_dir:
        isolated["PATH"] = _prepend_path(wrapper_dir, isolated.get("PATH", ""))
    isolated.pop("TMUX", None)
    isolated.pop("TMUX_PANE", None)
    return isolated


def _write_tmux_wrapper(path: Path) -> None:
    path.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "test -n \"${PYTEST_TMUX_SOCKET:-}\" || { "
        "echo 'pytest tmux socket is missing' >&2; exit 97; }\n"
        "test -n \"${PYTEST_REAL_TMUX:-}\" || { "
        "echo 'pytest real tmux path is missing' >&2; exit 97; }\n"
        "unset TMUX TMUX_PANE\n"
        "scan_global=true\n"
        "expect_value=\n"
        "explicit_socket=false\n"
        "for argument do\n"
        "  if test \"$scan_global\" != true; then continue; fi\n"
        "  if test -n \"$expect_value\"; then\n"
        "    if test \"$expect_value\" = '-S'; then\n"
        "      test \"$argument\" = \"$PYTEST_TMUX_SOCKET\" || { "
        "echo 'pytest refused another tmux socket' >&2; exit 97; }\n"
        "      explicit_socket=true\n"
        "    fi\n"
        "    expect_value=\n"
        "    continue\n"
        "  fi\n"
        "  case \"$argument\" in\n"
        "    --) scan_global=false ;;\n"
        "    -L|-L*) echo 'pytest refused a named tmux server' >&2; exit 97 ;;\n"
        "    -S) expect_value=-S ;;\n"
        "    -S*) echo 'pytest refused an attached tmux socket option' >&2; exit 97 ;;\n"
        "    -c|-f|-T) expect_value=$argument ;;\n"
        "    -*) ;;\n"
        "    *) scan_global=false ;;\n"
        "  esac\n"
        "done\n"
        "test -z \"$expect_value\" || { "
        "echo 'pytest refused an incomplete tmux option' >&2; exit 97; }\n"
        "if test \"$explicit_socket\" = true; then\n"
        "  exec \"$PYTEST_REAL_TMUX\" \"$@\"\n"
        "fi\n"
        "exec \"$PYTEST_REAL_TMUX\" -S \"$PYTEST_TMUX_SOCKET\" \"$@\"\n",
        encoding="utf-8",
    )
    path.chmod(0o700)


def _isolated_tmux_cleanup_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("TMUX", None)
    environment.pop("TMUX_PANE", None)
    return environment


def _isolated_tmux_run(args: Any, *pargs: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
    environment = _isolated_tmux_environment(kwargs.get("env"))
    kwargs["env"] = environment
    return _REAL_SUBPROCESS_RUN(
        _isolated_tmux_command(args, environment=environment),
        *pargs,
        **kwargs,
    )


@pytest.fixture(autouse=True)
def isolate_test_tmux(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
):
    """Force every test and child process onto one explicit private tmux socket."""

    real_tmux = shutil.which("tmux")
    runtime_root = tmp_path_factory.mktemp("termroom-test-runtime")
    socket_root = runtime_root / "tmux"
    socket_root.mkdir()
    socket_root.chmod(0o700)
    socket_path = socket_root / "tmux.sock"
    wrapper_dir = runtime_root / "bin"
    wrapper_dir.mkdir()
    if real_tmux:
        _write_tmux_wrapper(wrapper_dir / "tmux")

    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setenv("TMUX_TMPDIR", str(socket_root))
    monkeypatch.setenv("PYTEST_TMUX_SOCKET", str(socket_path))
    monkeypatch.setenv("PYTEST_TMUX_WRAPPER_DIR", str(wrapper_dir))
    if real_tmux:
        monkeypatch.setenv("PYTEST_REAL_TMUX", str(Path(real_tmux).resolve()))
        monkeypatch.setenv(
            "PATH",
            _prepend_path(str(wrapper_dir), os.environ.get("PATH", "")),
        )
    else:
        monkeypatch.delenv("PYTEST_REAL_TMUX", raising=False)

    monkeypatch.setattr(subprocess, "run", _isolated_tmux_run)
    monkeypatch.setattr(subprocess, "Popen", _IsolatedTmuxPopen)
    try:
        yield
    finally:
        if real_tmux:
            _REAL_SUBPROCESS_RUN(
                [str(Path(real_tmux).resolve()), "-S", str(socket_path), "kill-server"],
                check=False,
                capture_output=True,
                env=_isolated_tmux_cleanup_environment(),
            )
