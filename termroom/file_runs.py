from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import threading
import time
import uuid
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from termroom.db import FILE_RUN_TERMINAL_STATES, StateStore, utc_now
from termroom.files import (
    FileConflictError,
    FileService,
    RunnableFile,
    UnsupportedFileError,
)
from termroom.remote_access import RemoteAccess, RemoteAccessError
from termroom.ssh_backend import SSHBackend, SSHBackendError
from termroom.terminals import TerminalError, TerminalManager
from termroom.workspaces import WorkspaceManager

RUNNER_REGISTRY_VERSION = 1
FILE_RUN_OBSERVER_INTERVAL = 1.0
FILE_RUN_OBSERVER_MAX_BACKOFF = 30.0
NODE_FILE_RUN_BRIDGE_TIMEOUT = 35.0


class FileRunError(RuntimeError):
    def __init__(self, message: str, *, code: str, **values: Any) -> None:
        super().__init__(message)
        self.code = code
        self.values = values


class FileRunConflict(FileRunError):
    pass


@dataclass(frozen=True, slots=True)
class RunnerSpec:
    id: str
    suffixes: tuple[str, ...]
    program: str
    prefix: tuple[str, ...]
    runtime_error_code: str


@dataclass(frozen=True, slots=True)
class ResolvedRunner:
    id: str
    version: int
    argv: tuple[str, ...]
    runtime_error_code: str


RUNNER_SPECS = (
    RunnerSpec("python3", (".py",), "python3", ("--",), "python3_missing"),
    RunnerSpec(
        "node",
        (".js", ".mjs", ".cjs"),
        "node",
        ("--",),
        "nodejs_missing",
    ),
    RunnerSpec(
        "bash",
        (".sh", ".bash"),
        "bash",
        ("--noprofile", "--norc", "--"),
        "bash_missing",
    ),
)


def _suffix_registry() -> dict[str, RunnerSpec]:
    result: dict[str, RunnerSpec] = {}
    for spec in RUNNER_SPECS:
        for suffix in spec.suffixes:
            if suffix in result:
                raise RuntimeError(f"Duplicate File Runner suffix: {suffix}")
            result[suffix] = spec
    return result


SUFFIX_RUNNERS = _suffix_registry()


def resolve_runner(runnable: RunnableFile) -> ResolvedRunner | None:
    relative = PurePosixPath(runnable.relative_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("File Run path must be normalized and Workspace-relative")
    executable_path = f"./{relative.as_posix()}"
    if runnable.executable and runnable.has_shebang:
        return ResolvedRunner(
            id="direct",
            version=RUNNER_REGISTRY_VERSION,
            argv=(executable_path,),
            runtime_error_code="direct_runner_failed",
        )
    spec = SUFFIX_RUNNERS.get(relative.suffix)
    if spec is None:
        return None
    return ResolvedRunner(
        id=spec.id,
        version=RUNNER_REGISTRY_VERSION,
        argv=(spec.program, *spec.prefix, executable_path),
        runtime_error_code=spec.runtime_error_code,
    )


class FileRunManager:
    def __init__(
        self,
        store: StateStore,
        workspaces: WorkspaceManager,
        files: FileService,
        terminals: TerminalManager,
        ssh: SSHBackend,
        *,
        state_dir: Path,
        max_edit_bytes: int,
        remote: RemoteAccess | None = None,
    ) -> None:
        self.store = store
        self.workspaces = workspaces
        self.files = files
        self.terminals = terminals
        self.ssh = ssh
        self.remote = remote
        self.max_edit_bytes = max_edit_bytes
        self.metadata_root = (state_dir / "file-runs").resolve()
        self.metadata_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.metadata_root.chmod(0o700)
        self._workspace_locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._startup_task: asyncio.Task[None] | None = None
        self._observer_task: asyncio.Task[None] | None = None
        self._observer_wakeup = asyncio.Event()
        self._event_loop: asyncio.AbstractEventLoop | None = None

    def lock_for_workspace(self, workspace_id: str) -> threading.RLock:
        safe_id = self._validate_workspace_id(workspace_id)
        with self._locks_guard:
            return self._workspace_locks.setdefault(safe_id, threading.RLock())

    @staticmethod
    def _validate_uuid(value: str, label: str) -> str:
        try:
            parsed = uuid.UUID(str(value))
        except (AttributeError, ValueError) as exc:
            raise ValueError(f"{label} is invalid") from exc
        canonical = str(parsed)
        if parsed.version != 4 or canonical != value:
            raise ValueError(f"{label} is invalid")
        return canonical

    @staticmethod
    def _validate_workspace_id(value: str) -> str:
        workspace_id = str(value)
        if not workspace_id or any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for character in workspace_id
        ):
            raise ValueError("Workspace id is invalid")
        return workspace_id

    def _eligible_workspace(self, workspace: Mapping[str, Any]) -> None:
        if (
            str(workspace.get("workspace_kind") or "workspace") != "workspace"
            or bool(workspace.get("transient"))
        ):
            raise FileRunError(
                "Current-file execution is available only in persistent project Workspaces",
                code="workspace_not_supported",
            )
        if workspace.get("backend_kind") == "remote":
            computer = workspace.get("computer") or {}
            method = computer.get("connection_method")
            if method == "node":
                if self.remote is None or not self.remote.supports_capability(
                    workspace, "file_run"
                ):
                    raise FileRunError(
                        "This Remote does not support current-file execution yet",
                        code="workspace_not_supported",
                    )
            elif method != "ssh":
                raise FileRunError(
                    "This Remote does not support current-file execution yet",
                    code="workspace_not_supported",
                )

    def _call_node(
        self, factory: Callable[[], Coroutine[Any, Any, Any]]
    ) -> Any:
        loop = self._event_loop
        if loop is None or loop.is_closed():
            raise RemoteAccessError(
                "Node File Run bridge is unavailable", code="node_offline"
            )
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is loop:
            raise RuntimeError("Node File Run operations must run outside the Core loop")
        future = asyncio.run_coroutine_threadsafe(factory(), loop)
        try:
            return future.result(timeout=NODE_FILE_RUN_BRIDGE_TIMEOUT)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise RemoteAccessError(
                "Node File Run request timed out", code="node_offline"
            ) from exc

    def _is_node_workspace(self, workspace: Mapping[str, Any]) -> bool:
        return self.remote is not None and self.remote.is_node(workspace)

    def _metadata_dir(self, workspace_id: str, run_id: str) -> Path:
        self._validate_uuid(run_id, "File Run id")
        safe_workspace_id = self._validate_workspace_id(workspace_id)
        target = self.metadata_root / safe_workspace_id / run_id
        target.relative_to(self.metadata_root)
        return target

    def _inspect(
        self,
        workspace: dict[str, Any],
        relative_path: str,
        *,
        expected_digest: str | None = None,
    ) -> RunnableFile:
        self._eligible_workspace(workspace)
        if workspace.get("backend_kind") == "remote":
            if self._is_node_workspace(workspace):
                assert self.remote is not None
                return self._call_node(
                    lambda: self.remote.inspect_runnable(
                        workspace,
                        relative_path,
                        expected_digest=expected_digest,
                        max_bytes=self.max_edit_bytes,
                        runner_registry_version=RUNNER_REGISTRY_VERSION,
                    )
                )
            return self.ssh.inspect_runnable(
                workspace,
                relative_path,
                expected_digest=expected_digest,
                max_bytes=self.max_edit_bytes,
            )
        return self.files.inspect_runnable(
            Path(workspace["path"]),
            relative_path,
            expected_digest=expected_digest,
        )

    def runner_for_file(
        self, workspace: dict[str, Any], relative_path: str
    ) -> ResolvedRunner | None:
        return resolve_runner(self._inspect(workspace, relative_path))

    def get(self, run_id: str) -> dict[str, Any]:
        safe_id = self._validate_uuid(run_id, "File Run id")
        run = self.store.get_file_run(safe_id)
        if run is None:
            raise KeyError(f"Unknown File Run: {safe_id}")
        return run

    def active_for_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        return self.store.get_active_file_run(workspace_id)

    def latest_for_file(
        self, workspace_id: str, relative_path: str
    ) -> dict[str, Any] | None:
        return self.store.get_latest_file_run(workspace_id, relative_path)

    def start(
        self,
        workspace: dict[str, Any],
        relative_path: str,
        *,
        expected_digest: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._eligible_workspace(workspace)
        workspace_id = self._validate_workspace_id(str(workspace["id"]))
        with self.lock_for_workspace(workspace_id):
            return self._start_locked(
                workspace,
                relative_path,
                expected_digest=expected_digest,
                idempotency_key=idempotency_key,
            )

    def _start_locked(
        self,
        workspace: dict[str, Any],
        relative_path: str,
        *,
        expected_digest: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._eligible_workspace(workspace)
        safe_key = self._validate_uuid(idempotency_key, "Idempotency key")
        runnable = self._inspect(
            workspace, relative_path, expected_digest=expected_digest
        )
        runner = resolve_runner(runnable)
        if runner is None:
            raise FileRunError(
                "This file does not have a supported current-file Runner",
                code="runner_not_supported",
            )
        run_id = str(uuid.uuid4())
        claim, claimed = self.store.claim_file_run(
            {
                "id": run_id,
                "workspace_id": str(workspace["id"]),
                "idempotency_key": safe_key,
                "relative_path": runnable.relative_path,
                "source_digest": runnable.digest,
                "runner_id": runner.id,
                "runner_version": runner.version,
                "argv": runner.argv,
            }
        )
        if claim == "idempotent":
            if claimed["state"] not in FILE_RUN_TERMINAL_STATES:
                return self.reconcile(str(claimed["id"]))
            return self.get(str(claimed["id"]))
        if claim == "occupied":
            raise FileRunConflict(
                "Another file is already running in this Workspace",
                code="slot_occupied",
                active_run=claimed,
            )

        dispatch_attempted = False
        try:
            confirmed = self._inspect(
                workspace,
                runnable.relative_path,
                expected_digest=runnable.digest,
            )
            confirmed_runner = resolve_runner(confirmed)
            if confirmed_runner != runner:
                raise FileConflictError("The file Runner changed before execution")
            dispatch_attempted = True
            if workspace.get("backend_kind") == "remote":
                if self._is_node_workspace(workspace):
                    assert self.remote is not None
                    terminal = self._call_node(
                        lambda: self.remote.start_file_run(
                            workspace,
                            run_id=run_id,
                            relative_path=runnable.relative_path,
                            expected_digest=runnable.digest,
                            runner_id=runner.id,
                            runner_version=runner.version,
                            runner_registry_version=RUNNER_REGISTRY_VERSION,
                        )
                    )
                else:
                    terminal = self.ssh.start_file_run(
                        workspace,
                        run_id=run_id,
                        runner_id=runner.id,
                        runtime_error_code=runner.runtime_error_code,
                        argv=runner.argv,
                    )
            else:
                terminal = self.terminals.start_file_run(
                    workspace,
                    run_id=run_id,
                    runner_id=runner.id,
                    runtime_error_code=runner.runtime_error_code,
                    argv=runner.argv,
                    metadata_dir=self._metadata_dir(str(workspace["id"]), run_id),
                )
            self.store.set_file_run_terminal(run_id, str(terminal["id"]))
            return self.reconcile(run_id)
        except (FileConflictError, UnsupportedFileError, ValueError) as exc:
            self.store.transition_file_run(
                run_id,
                expected_states={"preparing"},
                state="failed",
                ended_at=utc_now(),
                error_code="source_changed",
                error_detail=str(exc),
            )
            return self.get(run_id)
        except (OSError, RemoteAccessError, SSHBackendError, TerminalError) as exc:
            if dispatch_attempted and workspace.get("backend_kind") == "remote":
                try:
                    observation = self._observe_backend(workspace, run_id)
                except (OSError, RemoteAccessError, SSHBackendError, TerminalError):
                    observation = None
                if observation is not None:
                    observed_state = str(observation.get("state") or "")
                    if observed_state == "lost":
                        self.store.transition_file_run(
                            run_id,
                            expected_states={"preparing"},
                            state="failed",
                            ended_at=utc_now(),
                            error_code="start_failed",
                            error_detail=str(exc),
                        )
                        return self.get(run_id)
                    current = self.get(run_id)
                    self._apply_observation(current, observation)
                    return {**self.get(run_id), "connection": "online"}
                self.store.transition_file_run(
                    run_id,
                    expected_states={"preparing"},
                    state="preparing",
                    error_code="start_status_unknown",
                    error_detail=str(exc),
                )
                return self.get(run_id)
            self.store.transition_file_run(
                run_id,
                expected_states={"preparing"},
                state="failed",
                ended_at=utc_now(),
                error_code="start_failed",
                error_detail=str(exc),
            )
            return self.get(run_id)

    def _observe_backend(
        self, workspace: dict[str, Any], run_id: str
    ) -> dict[str, Any]:
        if workspace.get("backend_kind") == "remote":
            if self._is_node_workspace(workspace):
                assert self.remote is not None
                return self._call_node(
                    lambda: self.remote.inspect_file_run(
                        workspace,
                        run_id=run_id,
                        runner_registry_version=RUNNER_REGISTRY_VERSION,
                    )
                )
            return self.ssh.inspect_file_run(workspace, run_id=run_id)
        return self.terminals.inspect_file_run(
            workspace,
            run_id=run_id,
            metadata_dir=self._metadata_dir(str(workspace["id"]), run_id),
        )

    def _apply_observation(
        self, run: Mapping[str, Any], observation: Mapping[str, Any]
    ) -> None:
        state = str(observation.get("state") or "")
        if state not in {
            "preparing",
            "running",
            "finished",
            "stopped",
            "failed",
            "lost",
        }:
            return
        current = str(run["state"])
        if current in FILE_RUN_TERMINAL_STATES:
            return
        if state == "preparing":
            return
        if state == "running":
            self.store.transition_file_run(
                str(run["id"]),
                expected_states={"preparing", "running"},
                state="running",
                started_at=observation.get("started_at")
                or run.get("started_at")
                or utc_now(),
                error_code=None,
                error_detail=None,
            )
            return
        self.store.transition_file_run(
            str(run["id"]),
            expected_states={"preparing", "running"},
            state=state,
            started_at=observation.get("started_at") or run.get("started_at"),
            ended_at=observation.get("ended_at") or utc_now(),
            exit_code=observation.get("exit_code"),
            error_code=observation.get("error_code"),
            error_detail=observation.get("error_detail"),
        )

    def reconcile(self, run_id: str) -> dict[str, Any]:
        safe_id = self._validate_uuid(run_id, "File Run id")
        initial = self.get(safe_id)
        with self.lock_for_workspace(str(initial["workspace_id"])):
            run = self.get(safe_id)
            if run["state"] in FILE_RUN_TERMINAL_STATES:
                return {**run, "connection": "online"}
            workspace = self.workspaces.require(str(run["workspace_id"]))
            try:
                observation = self._observe_backend(workspace, safe_id)
            except (RemoteAccessError, SSHBackendError):
                return {**run, "connection": "offline"}
            self._apply_observation(run, observation)
            return {**self.get(safe_id), "connection": "online"}

    def stop(self, run_id: str) -> dict[str, Any]:
        safe_id = self._validate_uuid(run_id, "File Run id")
        initial = self.get(safe_id)
        with self.lock_for_workspace(str(initial["workspace_id"])):
            run = self.reconcile(safe_id)
            if run["state"] in FILE_RUN_TERMINAL_STATES:
                return {"run": run, "needs_force": False}
            requested_at = str(run.get("stop_requested_at") or utc_now())
            workspace = self.workspaces.require(str(run["workspace_id"]))
            if workspace.get("backend_kind") == "remote":
                if self._is_node_workspace(workspace):
                    assert self.remote is not None
                    sent = self._call_node(
                        lambda: self.remote.interrupt_file_run(
                            workspace,
                            run_id=safe_id,
                            runner_registry_version=RUNNER_REGISTRY_VERSION,
                        )
                    )
                else:
                    sent = self.ssh.interrupt_file_run(workspace, run_id=safe_id)
            else:
                sent = self.terminals.interrupt_file_run(
                    workspace,
                    run_id=safe_id,
                    metadata_dir=self._metadata_dir(str(workspace["id"]), safe_id),
                )
            if not sent:
                updated = self.reconcile(safe_id)
                if updated["state"] in FILE_RUN_TERMINAL_STATES:
                    return {"run": updated, "needs_force": False}
                raise TerminalError("File Run interrupt could not be confirmed")
            self.store.transition_file_run(
                safe_id,
                expected_states={"preparing", "running"},
                stop_requested_at=requested_at,
            )
            time.sleep(0.25)
            updated = self.reconcile(safe_id)
            return {
                "run": updated,
                "needs_force": updated["state"] not in FILE_RUN_TERMINAL_STATES,
            }

    def kill(self, run_id: str) -> dict[str, Any]:
        safe_id = self._validate_uuid(run_id, "File Run id")
        initial = self.get(safe_id)
        with self.lock_for_workspace(str(initial["workspace_id"])):
            run = self.reconcile(safe_id)
            if run["state"] in FILE_RUN_TERMINAL_STATES:
                return run
            requested_at = str(run.get("stop_requested_at") or utc_now())
            workspace = self.workspaces.require(str(run["workspace_id"]))
            if workspace.get("backend_kind") == "remote":
                if self._is_node_workspace(workspace):
                    assert self.remote is not None
                    killed = self._call_node(
                        lambda: self.remote.kill_file_run(
                            workspace,
                            run_id=safe_id,
                            runner_registry_version=RUNNER_REGISTRY_VERSION,
                        )
                    )
                else:
                    killed = self.ssh.kill_file_run(workspace, run_id=safe_id)
            else:
                killed = self.terminals.kill_file_run(
                    workspace,
                    run_id=safe_id,
                    metadata_dir=self._metadata_dir(str(workspace["id"]), safe_id),
                )
            if killed:
                self.store.transition_file_run(
                    safe_id,
                    expected_states={"preparing", "running"},
                    stop_requested_at=requested_at,
                )
            time.sleep(0.1)
            updated = self.reconcile(safe_id)
            if killed and updated["state"] not in FILE_RUN_TERMINAL_STATES:
                self.store.transition_file_run(
                    safe_id,
                    expected_states={"preparing", "running"},
                    state="stopped",
                    ended_at=utc_now(),
                    error_code="forced",
                )
                updated = self.get(safe_id)
            return updated

    def wake(self) -> None:
        self._observer_wakeup.set()

    async def startup(self) -> None:
        self._event_loop = asyncio.get_running_loop()
        if not self._startup_task or self._startup_task.done():
            self._startup_task = asyncio.create_task(self._reconcile_startup())
        if not self._observer_task or self._observer_task.done():
            self._observer_task = asyncio.create_task(self._observe_active_runs())
        self._observer_wakeup.set()

    async def _reconcile_startup(self) -> None:
        for run in await asyncio.to_thread(self.store.list_active_file_runs):
            with contextlib.suppress(
                KeyError,
                OSError,
                RemoteAccessError,
                SSHBackendError,
                TerminalError,
                ValueError,
            ):
                await asyncio.to_thread(self.reconcile, str(run["id"]))
        self._observer_wakeup.set()

    async def _observe_active_runs(self) -> None:
        loop = asyncio.get_running_loop()
        next_attempt: dict[str, float] = {}
        failures: dict[str, int] = {}
        while True:
            self._observer_wakeup.clear()
            runs = await asyncio.to_thread(self.store.list_active_file_runs)
            active_ids = {str(run["id"]) for run in runs}
            for run_id in set(next_attempt) - active_ids:
                next_attempt.pop(run_id, None)
                failures.pop(run_id, None)
            now = loop.time()
            for run in runs:
                run_id = str(run["id"])
                if next_attempt.get(run_id, 0.0) > now:
                    continue
                try:
                    observed = await asyncio.to_thread(self.reconcile, run_id)
                except (
                    KeyError,
                    OSError,
                    RemoteAccessError,
                    SSHBackendError,
                    TerminalError,
                    ValueError,
                ):
                    connection = "offline"
                else:
                    connection = str(observed.get("connection") or "online")
                    if observed["state"] in FILE_RUN_TERMINAL_STATES:
                        next_attempt.pop(run_id, None)
                        failures.pop(run_id, None)
                        continue
                if connection == "offline":
                    count = failures.get(run_id, 0) + 1
                    failures[run_id] = count
                    delay = min(
                        FILE_RUN_OBSERVER_MAX_BACKOFF,
                        FILE_RUN_OBSERVER_INTERVAL * (2 ** min(count - 1, 4)),
                    )
                else:
                    failures.pop(run_id, None)
                    delay = FILE_RUN_OBSERVER_INTERVAL
                next_attempt[run_id] = loop.time() + delay
            if self._observer_wakeup.is_set():
                continue
            if not runs:
                await self._observer_wakeup.wait()
                continue
            due = [next_attempt.get(str(run["id"]), loop.time()) for run in runs]
            timeout = max(0.0, min(due) - loop.time())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._observer_wakeup.wait(), timeout=timeout)

    async def shutdown(self) -> None:
        tasks = [
            task
            for task in (self._startup_task, self._observer_task)
            if task and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._event_loop = None
