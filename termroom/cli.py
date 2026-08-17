from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn

from termroom.app import create_app
from termroom.assets import ensure_xterm_assets
from termroom.config import Settings, default_state_dir
from termroom.db import StateStore
from termroom.node_agent import (
    NodeAgent,
    NodeAgentError,
    NodePermanentError,
    ensure_node_identity,
    load_node_config,
    load_node_identity,
    pair_node,
)
from termroom.node_protocol import public_key_fingerprint, public_key_text
from termroom.node_service import (
    NODE_PERMANENT_EXIT_STATUS,
    NodeProcessLock,
    NodeServiceError,
    NodeServiceManager,
    NodeServiceStatus,
    write_node_runtime_status,
)
from termroom.runtime import runtime_fingerprint
from termroom.terminals import TerminalManager
from termroom.workspaces import RootManager, WorkspaceManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="termroom",
        description="Open a Linux project as a persistent touch-first web workspace.",
    )
    parser.add_argument("root", nargs="?", default=".", help="Project or allowed root directory")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port")
    _add_config_dir_argument(parser)
    parser.add_argument(
        "--secure-cookie",
        action="store_true",
        help="Mark auth cookies Secure (use behind HTTPS)",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the local browser automatically",
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Run the web Core in the foreground (for Docker/systemd)",
    )
    parser.add_argument(
        "--allow-root",
        action="store_true",
        help="Allow running as the root OS user (strongly discouraged)",
    )
    return parser


def _build_attach_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="termroom attach", description="Attach to this Workspace")
    parser.add_argument("path", nargs="?", default=".")
    _add_config_dir_argument(parser)
    return parser


def _build_stop_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="termroom stop", description="Stop a Workspace or Core")
    parser.add_argument("path", nargs="?", default=".")
    _add_config_dir_argument(parser)
    parser.add_argument("--core", action="store_true", help="Stop the Termroom web Core")
    return parser


def _build_node_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="termroom node",
        description="Connect this Linux computer to a Termroom Core without inbound SSH.",
    )
    _add_config_dir_argument(parser)
    parser.add_argument(
        "--allow-root-user",
        action="store_true",
        help="Allow running the Node process as the root OS user (strongly discouraged)",
    )
    commands = parser.add_subparsers(dest="node_command")
    pair_parser = commands.add_parser(
        "pair", help="Pair this computer with a Termroom Core"
    )
    pair_parser.add_argument("--core", required=True, help="Termroom Core base URL")
    pair_parser.add_argument("--code", required=True, help="One-time pairing code")
    pair_parser.add_argument(
        "--ca-file",
        help="PEM CA bundle for this Core's HTTPS certificate",
    )
    pair_parser.add_argument(
        "--allow-root",
        action="append",
        required=True,
        dest="allowed_roots",
        metavar="PATH",
        help="Folder the Node may expose; repeat to allow more than one",
    )
    pair_parser.add_argument("--name", help="Computer name shown in Termroom")
    pair_parser.add_argument(
        "--run-root",
        help="Node-local folder for managed Remote Runs (default: Node state/runs)",
    )
    pair_parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="Seconds to wait for fingerprint approval (default: 600)",
    )
    commands.add_parser(
        "install-service", help="Install and start the systemd user service"
    )
    commands.add_parser("status", help="Show the Node user service and Core connection state")
    commands.add_parser(
        "uninstall-service", help="Stop and remove the systemd user service"
    )
    return parser


def _add_config_dir_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config-dir",
        "--state-dir",
        dest="state_dir",
        help="Persistent Termroom config directory (DB, SSH keys, credentials)",
    )


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "attach":
        attach_parser = _build_attach_parser()
        _require_tmux(attach_parser)
        _attach(attach_parser.parse_args(argv[1:]))
        return
    if argv and argv[0] == "stop":
        _stop(_build_stop_parser().parse_args(argv[1:]))
        return
    if argv and argv[0] == "node":
        node_parser = _build_node_parser()
        node_args = node_parser.parse_args(argv[1:])
        _validate_user(node_parser, node_args.allow_root_user)
        if node_args.node_command not in {"status", "uninstall-service"}:
            _require_tmux(node_parser)
        _run_node(node_parser, node_args)
        return
    if argv and argv[0] == "serve":
        argv = argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_user(parser, args.allow_root)
    _require_tmux(parser)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    try:
        settings = Settings.create(
            args.root,
            host=args.host,
            port=args.port,
            state_dir=args.state_dir,
            secure_cookie=args.secure_cookie,
        )
        ensure_xterm_assets()
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    existing_core = _read_core_metadata(settings.state_dir)
    if existing_core and _core_process_matches(existing_core):
        current_fingerprint = runtime_fingerprint()
        runtime_matches = _core_runtime_matches(existing_core, current_fingerprint)
        settings_match = _core_settings_match(existing_core, settings)
        if runtime_matches and settings_match:
            _open_in_running_core(parser, args, settings, existing_core)
            return
        _adopt_existing_core_options(args, existing_core)
        try:
            old_pid = _stop_core_process(existing_core)
        except RuntimeError as exc:
            parser.error(str(exc))
        reason = "older code" if not runtime_matches else "updated settings"
        print(f"Termroom Core was using {reason}; restarting it (pid {old_pid}).", flush=True)
        settings = Settings.create(
            args.root,
            host=args.host,
            port=args.port,
            state_dir=args.state_dir,
            secure_cookie=args.secure_cookie,
        )
        existing_core = None
    if existing_core:
        with contextlib.suppress(FileNotFoundError):
            _core_metadata_path(settings.state_dir).unlink()

    if not settings.login_password:
        parser.error(
            "Termroom login password is not configured. Add `TERMROOM_PASSWORD=...` "
            f"to {settings.state_dir / '.env'} or the environment."
        )
    if not args.foreground:
        _start_background_core(parser, args, settings)
        metadata = _wait_for_core(settings.state_dir, timeout=8.0)
        if not metadata:
            raise SystemExit(
                "Termroom Core did not start. Run `termroom . --foreground` to see startup errors."
            )
        _open_in_running_core(parser, args, settings, metadata, started_now=True)
        return

    _scrub_runtime_secrets()
    app = create_app(settings)
    local_base = f"http://127.0.0.1:{settings.port}"
    shown_base = _display_base_url(settings.host, settings.port)
    if settings.allow_local_workspaces:
        workspace = app.state.workspaces.open(".")
        app.state.terminals.ensure_workspace(workspace)
        workspace_id = str(workspace["id"])
        browser_url = f"{local_base}/w/{workspace_id}"
        shown_url = f"{shown_base}/w/{workspace_id}"
    else:
        workspace = None
        workspace_id = ""
        browser_url = f"{local_base}/"
        shown_url = f"{shown_base}/"

    _write_core_metadata(settings, workspace_id)
    print("Termroom is running", flush=True)
    if workspace is not None:
        print(f"Workspace: {workspace['path']}", flush=True)
    else:
        print("Mode:      SSH / Node Workspaces only", flush=True)
    print(f"Local:     {browser_url}", flush=True)
    print(f"Open:      {shown_url}", flush=True)
    print("Login:     password required", flush=True)
    if settings.host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            "Access is exposed beyond localhost. Prefer a private VPN or HTTPS proxy.",
            flush=True,
        )

    if not args.no_open:
        _open_browser(browser_url)

    try:
        uvicorn.run(
            app,
            host=settings.host,
            port=settings.port,
            log_level="info",
        )
    finally:
        _remove_core_metadata_if_current(settings.state_dir)


def _scrub_runtime_secrets() -> None:
    """Keep loaded credentials out of the Core's inherited process environment."""

    os.environ.pop("TERMROOM_PASSWORD", None)


def _open_in_running_core(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    settings: Settings,
    metadata: dict[str, Any],
    *,
    started_now: bool = False,
) -> None:
    existing_port = int(metadata.get("port", 8765))
    existing_host = str(metadata.get("host", "127.0.0.1"))
    if args.port != 8765 and args.port != existing_port:
        parser.error(
            f"Termroom Core is already running on port {existing_port}. "
            "Stop it with `termroom stop --core` before changing ports."
        )
    if args.host != "127.0.0.1" and args.host != existing_host:
        parser.error(
            f"Termroom Core is already bound to {existing_host}. "
            "Stop it with `termroom stop --core` before changing bind address."
        )

    base = _display_base_url(existing_host, existing_port)
    if settings.allow_local_workspaces:
        store = StateStore(settings.database_path)
        store.initialize()
        manager = WorkspaceManager(RootManager(settings.root), store)
        workspace = manager.open(".")
        TerminalManager(store).ensure_workspace(workspace)
        url = f"{base}/w/{workspace['id']}"
    else:
        workspace = None
        url = f"{base}/"
    status = (
        "Termroom Core started in the background"
        if started_now
        else "Termroom Core is already running"
    )
    print(status, flush=True)
    if workspace is not None:
        print(f"Workspace: {workspace['path']}", flush=True)
    else:
        print("Mode:      SSH / Node Workspaces only", flush=True)
    print(f"Open:      {url}", flush=True)
    print("Login:     password required", flush=True)
    if existing_host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            "Access is exposed beyond localhost. Prefer a private VPN or HTTPS proxy.",
            flush=True,
        )
    if not args.no_open:
        _open_browser(url)


def _run_node(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    state_dir = _node_state_path(args.state_dir)
    try:
        if args.node_command == "pair":
            private_key = ensure_node_identity(state_dir)
            fingerprint = public_key_fingerprint(public_key_text(private_key.public_key()))
            print(f"Node fingerprint: {fingerprint}", flush=True)
            print("Waiting for approval in Termroom Core...", flush=True)
            config = pair_node(
                state_dir=state_dir,
                core_url=args.core,
                code=args.code,
                allowed_roots=args.allowed_roots,
                name=args.name,
                run_root=args.run_root,
                ca_file=args.ca_file,
                timeout_seconds=args.timeout,
            )
            print(f"Node paired: {config.name} ({config.node_id})", flush=True)
            print("Run `termroom node` to connect.", flush=True)
            return
        if args.node_command == "status":
            _print_node_service_status(NodeServiceManager(state_dir).status())
            return
        if args.node_command == "uninstall-service":
            status = NodeServiceManager(state_dir).uninstall()
            print("Termroom Node service removed.", flush=True)
            _print_node_service_status(status)
            print("Node identity and allowed roots were preserved.", flush=True)
            return
        config = load_node_config(state_dir)
        private_key = load_node_identity(state_dir)
        fingerprint = public_key_fingerprint(public_key_text(private_key.public_key()))
        if args.node_command == "install-service":
            manager = NodeServiceManager(state_dir)
            status = manager.install(_node_service_command(state_dir))
            print("Termroom Node service installed and started.", flush=True)
            _print_node_service_status(status)
            if status.linger == "disabled":
                print(
                    "Automatic start before login requires lingering. Ask an administrator to run: "
                    f"loginctl enable-linger {os.getuid()}",
                    flush=True,
                )
            elif status.linger == "unknown":
                print(
                    "Could not determine lingering. The service starts at login; an administrator "
                    "can enable lingering for start before login.",
                    flush=True,
                )
            return
        agent = NodeAgent(config, private_key)
    except (OSError, ValueError, NodeAgentError, NodeServiceError) as exc:
        parser.error(str(exc))

    print(f"Termroom Node: {config.name}", flush=True)
    print(f"Core:          {config.core_url}", flush=True)
    print(f"Fingerprint:   {fingerprint}", flush=True)
    print("Allowed roots:", flush=True)
    for root in config.allowed_roots:
        print(f"  {root}", flush=True)
    print(f"Remote Run root: {config.run_root}", flush=True)
    permanent_failure = False
    try:
        with NodeProcessLock(state_dir, config.node_id):
            write_node_runtime_status(state_dir, config.node_id, "starting")
            try:
                asyncio.run(_run_node_agent(agent))
            except KeyboardInterrupt:
                return
            except NodePermanentError as exc:
                permanent_failure = True
                print(f"Termroom Node stopped: {exc}", file=sys.stderr, flush=True)
                raise SystemExit(NODE_PERMANENT_EXIT_STATUS) from exc
            finally:
                if not permanent_failure:
                    write_node_runtime_status(state_dir, config.node_id, "stopped")
    except NodeServiceError as exc:
        parser.error(str(exc))


async def _run_node_agent(agent: NodeAgent) -> None:
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(agent.run_forever())
    stop_requested = False

    def request_stop() -> None:
        nonlocal stop_requested
        stop_requested = True
        task.cancel()

    signal_handler_installed = False
    with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
        loop.add_signal_handler(signal.SIGTERM, request_stop)
        signal_handler_installed = True
    try:
        await task
    except asyncio.CancelledError:
        if not stop_requested:
            raise
    finally:
        if signal_handler_installed:
            loop.remove_signal_handler(signal.SIGTERM)


def _node_service_command(state_dir: Path) -> list[str]:
    executable = Path(os.path.abspath(sys.executable))
    return [
        str(executable),
        "-m",
        "termroom.cli",
        "node",
        "--state-dir",
        str(state_dir),
    ]


def _print_node_service_status(status: NodeServiceStatus) -> None:
    print(f"Installed: {'yes' if status.installed else 'no'}", flush=True)
    print(f"Enabled:   {'yes' if status.enabled else 'no'}", flush=True)
    print(f"Active:    {'yes' if status.active else 'no'} ({status.service_state})", flush=True)
    print(f"Core:      {status.core_state}", flush=True)
    print(f"Lingering: {status.linger}", flush=True)
    if status.last_error_code:
        print(f"Last error: {status.last_error_code}", flush=True)


def _start_background_core(
    parser: argparse.ArgumentParser, args: argparse.Namespace, settings: Settings
) -> None:
    command = [
        sys.executable,
        "-m",
        "termroom.cli",
        str(settings.root),
        "--foreground",
        "--no-open",
        "--host",
        settings.host,
        "--port",
        str(settings.port),
        "--state-dir",
        str(settings.state_dir),
    ]
    if settings.secure_cookie:
        command.append("--secure-cookie")
    if getattr(args, "allow_root", False):
        command.append("--allow-root")

    log_path = settings.state_dir / "core.log"
    try:
        log_handle = log_path.open("ab", buffering=0)
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            cwd=str(settings.root),
        )
    except OSError as exc:
        parser.error(f"Could not start Termroom Core: {exc}")
    finally:
        with contextlib.suppress(UnboundLocalError):
            log_handle.close()


def _wait_for_core(state_dir: Path, *, timeout: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        metadata = _read_core_metadata(state_dir)
        if (
            metadata
            and _core_process_matches(metadata)
            and _core_accepting_connections(metadata)
        ):
            return metadata
        time.sleep(0.05)
    return None


def _core_accepting_connections(metadata: dict[str, Any]) -> bool:
    host = str(metadata.get("host", "127.0.0.1"))
    if host in {"0.0.0.0", "::", "localhost", "::1"}:
        host = "127.0.0.1"
    try:
        port = int(metadata.get("port", 8765))
    except (TypeError, ValueError):
        return False
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def _attach(args: argparse.Namespace) -> None:
    store = _open_existing_store(args.state_dir)
    workspace = store.find_workspace_for_path(Path(args.path))
    if not workspace:
        raise SystemExit(
            "No Termroom Workspace contains this path. Open it with `termroom .` first."
        )
    session = str(workspace["tmux_session"])
    if subprocess.run(
        ["tmux", "has-session", "-t", session], capture_output=True, check=False
    ).returncode:
        raise SystemExit(
            "The Workspace tmux session is not running. Open the Workspace in Termroom first."
        )
    os.execvp("tmux", ["tmux", "attach-session", "-t", session])


def _stop(args: argparse.Namespace) -> None:
    state_dir = _state_path(args.state_dir)
    if args.core:
        metadata = _read_core_metadata(state_dir)
        if not metadata:
            raise SystemExit("Termroom Core is not recorded as running.")
        try:
            pid = _stop_core_process(metadata)
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"Stopped Termroom Core (pid {pid}).")
        return

    if shutil.which("tmux") is None:
        raise SystemExit("tmux is required to stop a Workspace session. Install tmux and retry.")

    store = _open_existing_store(args.state_dir)
    workspace = store.find_workspace_for_path(Path(args.path))
    if not workspace:
        raise SystemExit("No Termroom Workspace contains this path.")
    result = subprocess.run(
        ["tmux", "kill-session", "-t", str(workspace["tmux_session"])],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise SystemExit("The Workspace session is already stopped.")
    print(f"Stopped Workspace session: {workspace['display_name']}")


def _validate_user(parser: argparse.ArgumentParser, allow_root: bool) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0 and not allow_root:
        parser.error("Termroom refuses to run as root. Use a dedicated non-root user.")


def _require_tmux(parser: argparse.ArgumentParser) -> None:
    if shutil.which("tmux") is None:
        parser.error(
            "tmux is required for persistent Termroom terminals. Install tmux and retry."
        )


def _state_path(value: str | None) -> Path:
    return (Path(value).expanduser() if value else default_state_dir()).resolve()


def _node_state_path(value: str | None) -> Path:
    path = Path(value).expanduser() if value else default_state_dir() / "node"
    return path.resolve(strict=False)


def _open_existing_store(state_dir_value: str | None) -> StateStore:
    state_dir = _state_path(state_dir_value)
    database = state_dir / "termroom.sqlite3"
    if not database.exists():
        raise SystemExit(f"Termroom state database not found: {database}")
    store = StateStore(database)
    store.initialize()
    return store


def _display_base_url(host: str, port: int) -> str:
    if host in {"0.0.0.0", "::"}:
        host = _local_address()
    elif host in {"localhost", "::1"}:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}"


def _local_address() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
    except OSError:
        return socket.gethostname()


def _open_browser(url: str) -> None:
    # On headless Linux, webbrowser can choose console-oriented helpers. Avoid
    # surprising users and simply leave the printed access URL instead.
    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return
    try:
        webbrowser.open(url, new=2)
    except webbrowser.Error:
        return


def _core_metadata_path(state_dir: Path) -> Path:
    return state_dir / "core.json"


def _write_core_metadata(settings: Settings, workspace_id: str) -> None:
    data = {
        "pid": os.getpid(),
        "pid_start_ticks": _pid_start_ticks(os.getpid()),
        "runtime_fingerprint": runtime_fingerprint(),
        "root": str(settings.root),
        "state_dir": str(settings.state_dir),
        "host": settings.host,
        "port": settings.port,
        "secure_cookie": settings.secure_cookie,
        "default_locale": settings.default_locale,
        "allow_local_workspaces": settings.allow_local_workspaces,
        "workspace_id": workspace_id,
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    path = _core_metadata_path(settings.state_dir)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _read_core_metadata(state_dir: Path) -> dict[str, Any] | None:
    path = _core_metadata_path(state_dir)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _core_process_matches(metadata: dict[str, Any]) -> bool:
    try:
        pid = int(metadata.get("pid", 0))
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    expected_ticks = str(metadata.get("pid_start_ticks", ""))
    actual_ticks = _pid_start_ticks(pid)
    if not actual_ticks:
        return False
    return not expected_ticks or actual_ticks == expected_ticks


def _core_runtime_matches(
    metadata: dict[str, Any], current_fingerprint: str | None = None
) -> bool:
    expected = str(metadata.get("runtime_fingerprint", ""))
    if not expected:
        return False
    return expected == (current_fingerprint or runtime_fingerprint())


def _core_settings_match(metadata: dict[str, Any], settings: Settings) -> bool:
    return (
        str(metadata.get("default_locale", "")) == settings.default_locale
        and bool(metadata.get("allow_local_workspaces", True))
        == settings.allow_local_workspaces
    )


def _adopt_existing_core_options(args: argparse.Namespace, metadata: dict[str, Any]) -> None:
    if args.port == 8765:
        with contextlib.suppress(TypeError, ValueError):
            args.port = int(metadata.get("port", args.port))
    if args.host == "127.0.0.1":
        args.host = str(metadata.get("host", args.host))
    if not args.secure_cookie and bool(metadata.get("secure_cookie", False)):
        args.secure_cookie = True


def _stop_core_process(metadata: dict[str, Any], *, timeout: float = 5.0) -> int:
    try:
        pid = int(metadata.get("pid", 0))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Termroom Core metadata does not contain a valid process ID.") from exc
    expected_ticks = str(metadata.get("pid_start_ticks", ""))
    if pid <= 0 or (expected_ticks and _pid_start_ticks(pid) != expected_ticks):
        raise RuntimeError("Termroom Core metadata is stale; no matching process was stopped.")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError as exc:
        raise RuntimeError("Termroom Core is no longer running.") from exc
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _core_process_matches(metadata):
            break
        time.sleep(0.05)
    else:
        raise RuntimeError(
            f"Termroom Core did not stop within {timeout:g} seconds. "
            "Check the process before retrying."
        )
    state_dir_value = metadata.get("state_dir")
    if state_dir_value:
        with contextlib.suppress(FileNotFoundError):
            _core_metadata_path(Path(str(state_dir_value))).unlink()
    return pid


def _remove_core_metadata_if_current(state_dir: Path) -> None:
    metadata = _read_core_metadata(state_dir)
    if not metadata or int(metadata.get("pid", -1)) != os.getpid():
        return
    with contextlib.suppress(FileNotFoundError):
        _core_metadata_path(state_dir).unlink()


def _pid_start_ticks(pid: int) -> str:
    try:
        parts = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    except OSError:
        return ""
    return parts[21] if len(parts) > 21 else ""


if __name__ == "__main__":
    main()
