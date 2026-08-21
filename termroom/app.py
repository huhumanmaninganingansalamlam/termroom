from __future__ import annotations

import asyncio
import contextlib
import csv
import hashlib
import io
import ipaddress
import json
import os
import secrets
import tempfile
import uuid
import zipfile
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Annotated, Any
from urllib.parse import quote, urlencode, urlparse

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from starlette.datastructures import Headers, UploadFile
from starlette.middleware.gzip import GZipMiddleware
from starlette.staticfiles import NotModifiedResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from termroom.auth import AuthManager, AuthRateLimited
from termroom.config import Settings
from termroom.db import StateStore
from termroom.file_runs import FileRunConflict, FileRunError, FileRunManager
from termroom.files import (
    DEFAULT_FILE_BROWSER_NOISE,
    FileConflictError,
    FileService,
    FileSnapshot,
    RecentFiles,
    UnsupportedFileError,
    editor_newline_style,
    normalize_editor_newlines,
)
from termroom.i18n import (
    LOCALE_COOKIE,
    SUPPORTED_LOCALES,
    locale_from_request,
    localize_error_code,
    localize_exception,
    normalize_locale,
    template_context,
    translate,
)
from termroom.node_core import NodeCore, NodePairingRateLimiter
from termroom.node_protocol import (
    PAIRING_CODE_TTL_SECONDS,
    NodeProtocolError,
    generate_pairing_code,
    pairing_code_digest,
    public_key_fingerprint,
    secret_digest,
    validate_protocol_version,
)
from termroom.node_remote_runs import NodeRemoteRunClient
from termroom.pwa_icon import PWA_ICON_VERSION, termroom_png_icon
from termroom.remote_access import (
    RemoteAccess,
    RemoteAccessError,
)
from termroom.remote_runs import (
    TERMINAL_STATES,
    RemoteRunConflict,
    RemoteRunError,
    RemoteRunManager,
)
from termroom.run_results import (
    RemoteRunResultCollector,
    ResultCollectionConflict,
    ResultCollectionError,
)
from termroom.run_sources import normalize_source_relative_path
from termroom.runtime import runtime_stamp
from termroom.security import (
    PathBoundaryError,
    file_digest,
    is_within,
    resolve_inside,
    secure_compare,
)
from termroom.ssh_backend import SSHBackend, SSHBackendError
from termroom.terminal_control import TerminalControl
from termroom.terminals import TerminalError, TerminalManager, workspace_command_digest
from termroom.workspace_usage import WorkspaceUsageOffline, WorkspaceUsageService
from termroom.workspaces import (
    ProjectCreatedButWorkspaceFailed,
    ProjectNameError,
    ProjectPathExists,
    RootManager,
    WorkspaceManager,
)

PACKAGE_ROOT = Path(__file__).resolve().parent
FILE_BROWSER_PAGE_SIZE = 200
MAX_INLINE_IMAGE_BYTES = 25 * 1024 * 1024
MAX_INLINE_PDF_BYTES = 100 * 1024 * 1024
FILE_RUN_ERROR_KEYS = {
    "workspace_not_supported": "file_run.error.workspace_not_supported",
    "runner_not_supported": "file_run.error.runner_not_supported",
    "python3_missing": "file_run.error.python3_missing",
    "nodejs_missing": "file_run.error.nodejs_missing",
    "bash_missing": "file_run.error.bash_missing",
    "direct_runner_failed": "file_run.error.direct_runner_failed",
    "source_changed": "file_run.error.source_changed",
    "start_status_unknown": "file_run.error.start_status_unknown",
    "start_failed": "file_run.error.start_failed",
    "managed_terminal_missing": "file_run.error.managed_terminal_missing",
    "completion_missing": "file_run.error.completion_missing",
    "runner_metadata_invalid": "file_run.error.start_failed",
}
RESULT_COLLECTION_ERROR_KEYS = {
    "result_not_ready": "remote_run.collect.error.not_ready",
    "result_workspace_unavailable": "remote_run.collect.error.unavailable",
    "result_too_large": "remote_run.collect.error.too_large",
    "result_changed": "remote_run.collect.error.changed",
    "source_collection_unsupported": "remote_run.collect.error.source_kind",
    "collection_baseline_unavailable": "remote_run.collect.error.baseline",
    "source_workspace_unavailable": "remote_run.collect.error.source_unavailable",
    "plan_revision_mismatch": "remote_run.collect.error.plan_changed",
    "collection_too_many_changes": "remote_run.collect.error.too_many_changes",
    "result_path_duplicate": "remote_run.collect.error.invalid_result",
    "result_too_many_entries": "remote_run.collect.error.invalid_result",
    "result_too_deep": "remote_run.collect.error.invalid_result",
    "result_metadata_invalid": "remote_run.collect.error.invalid_result",
    "result_path_invalid": "remote_run.collect.error.invalid_result",
    "result_read_invalid": "remote_run.collect.error.invalid_result",
}
RESULT_COLLECTION_REASON_KEYS = {
    "source_unchanged_since_run": "remote_run.collect.reason.safe_modified",
    "new_local_text_file": "remote_run.collect.reason.safe_added",
    "new_remote_text_file": "remote_run.collect.reason.safe_added",
    "source_matches_result": "remote_run.collect.reason.already_result",
    "result_matches_baseline": "remote_run.collect.reason.already_result",
    "deletion_not_applied": "remote_run.collect.reason.delete_skipped",
    "file_too_large": "remote_run.collect.reason.download_only",
    "unsupported_result_type": "remote_run.collect.reason.download_only",
    "binary_result": "remote_run.collect.reason.download_only",
    "non_utf8_result": "remote_run.collect.reason.download_only",
    "excluded_new_path": "remote_run.collect.reason.excluded",
    "source_parent_missing": "remote_run.collect.reason.parent_missing",
    "result_type_conflict": "remote_run.collect.reason.source_conflict",
    "baseline_type_conflict": "remote_run.collect.reason.source_conflict",
    "source_path_is_directory": "remote_run.collect.reason.source_conflict",
    "source_path_exists": "remote_run.collect.reason.source_conflict",
    "source_changed_since_run": "remote_run.collect.reason.source_changed",
    "source_changed_during_review": "remote_run.collect.reason.source_changed",
    "source_changed_during_apply": "remote_run.collect.reason.source_changed",
    "source_file_missing": "remote_run.collect.reason.source_missing",
    "source_path_unsupported": "remote_run.collect.reason.source_unsupported",
    "source_not_editable_text": "remote_run.collect.reason.source_unsupported",
    "applied": "remote_run.collect.reason.applied",
    "apply_failed": "remote_run.collect.reason.apply_failed",
}
templates = Jinja2Templates(
    directory=PACKAGE_ROOT / "templates",
    context_processors=[template_context],
)
templates.env.globals["pwa_icon_version"] = PWA_ICON_VERSION

_REMOTE_STATUS_FRESH_FOR = timedelta(minutes=1)


_COMPRESSIBLE_STATIC_SUFFIXES = frozenset({".css", ".js", ".json", ".svg", ".webmanifest"})
_STATIC_GZIP_MINIMUM_SIZE = 1024


def _static_request_suffix(scope: Scope) -> str:
    return PurePosixPath(str(scope.get("path", ""))).suffix.casefold()


def _static_request_has_range(scope: Scope) -> bool:
    return any(name.lower() == b"range" for name, _ in scope.get("headers", ()))


def _static_request_accepts_gzip(scope: Scope) -> bool:
    explicit_quality: float | None = None
    wildcard_quality: float | None = None
    raw_values = Headers(scope=scope).getlist("Accept-Encoding")
    for raw_value in raw_values:
        for item in raw_value.split(","):
            parts = [part.strip() for part in item.split(";")]
            coding = parts[0].casefold()
            quality = 1.0
            for parameter in parts[1:]:
                name, separator, value = parameter.partition("=")
                if separator and name.strip().casefold() == "q":
                    try:
                        quality = float(value.strip())
                    except ValueError:
                        quality = 0.0
                    if not 0 <= quality <= 1:
                        quality = 0.0
                    break
            if coding == "gzip":
                explicit_quality = max(explicit_quality or 0.0, quality)
            elif coding == "*":
                wildcard_quality = max(wildcard_quality or 0.0, quality)
    selected = explicit_quality if explicit_quality is not None else wildcard_quality
    return selected is not None and selected > 0


class _CacheAwareStaticFiles(StaticFiles):
    """Give compressed and identity representations distinct validators."""

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        request_headers = Headers(scope=scope)
        response = FileResponse(full_path, status_code=status_code, stat_result=stat_result)
        can_compress = (
            _static_request_suffix(scope) in _COMPRESSIBLE_STATIC_SUFFIXES
            and stat_result.st_size >= _STATIC_GZIP_MINIMUM_SIZE
        )
        if can_compress:
            response.headers.add_vary_header("Accept-Encoding")
        if (
            can_compress
            and scope.get("method") == "GET"
            and not _static_request_has_range(scope)
            and _static_request_accepts_gzip(scope)
        ):
            etag = response.headers["etag"]
            response.headers["etag"] = f'{etag[:-1]}-gzip"'
        if self.is_not_modified(response.headers, request_headers):
            return NotModifiedResponse(response.headers)
        return response


class _StaticGZipMiddleware:
    """Compress text assets without touching fonts, ranges, or private responses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.gzip_app = GZipMiddleware(
            app,
            minimum_size=_STATIC_GZIP_MINIMUM_SIZE,
            compresslevel=6,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method") == "GET"
            and not _static_request_has_range(scope)
            and _static_request_suffix(scope) in _COMPRESSIBLE_STATIC_SUFFIXES
            and _static_request_accepts_gzip(scope)
        ):
            await self.gzip_app(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_app(settings: Settings) -> FastAPI:
    if not settings.login_password:
        raise ValueError(
            "Termroom login password is not configured. Add `TERMROOM_PASSWORD=...` "
            f"to {settings.state_dir / '.env'} or the environment."
        )
    store = StateStore(settings.database_path)
    store.initialize()
    roots = RootManager(settings.root)
    workspaces = WorkspaceManager(
        roots,
        store,
        allow_local_workspaces=settings.allow_local_workspaces,
    )
    files = FileService(settings.max_edit_bytes)
    terminal_control = TerminalControl()
    terminals = TerminalManager(store, terminal_control)
    ssh = SSHBackend(
        store,
        settings.state_dir,
        terminal_control,
        reuse_connections=True,
    )
    node_core = NodeCore(store)
    node_remote_runs = NodeRemoteRunClient(node_core)
    node_pairing_limiter = NodePairingRateLimiter()
    remote = RemoteAccess(store, ssh, node_core, terminal_control)
    remote_runs = RemoteRunManager(
        store,
        workspaces,
        ssh,
        node_remote_runs,
        state_dir=settings.state_dir,
        max_archive_bytes=settings.max_upload_bytes,
    )
    run_results = RemoteRunResultCollector(
        remote_runs,
        workspaces,
        remote,
        files,
        state_dir=settings.state_dir,
        max_archive_bytes=settings.max_upload_bytes,
    )
    file_runs = FileRunManager(
        store,
        workspaces,
        files,
        terminals,
        ssh,
        state_dir=settings.state_dir,
        max_edit_bytes=settings.max_edit_bytes,
        remote=remote,
    )
    workspace_usage = WorkspaceUsageService()
    auth = AuthManager(settings)
    active_websockets: dict[str, list[WebSocket]] = {}
    active_terminal_websockets: dict[str, list[WebSocket]] = {}
    recent_file_snapshots: dict[str, dict[str, tuple[int, int]]] = {}
    recent_file_cache: dict[str, tuple[list[dict[str, Any]], RecentFiles]] = {}
    terminal_activity_refreshes: dict[
        tuple[str, str], tuple[frozenset[str], asyncio.Task[None]]
    ] = {}

    app = FastAPI(title="Termroom", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.store = store
    app.state.roots = roots
    app.state.workspaces = workspaces
    app.state.files = files
    app.state.terminals = terminals
    app.state.terminal_control = terminal_control
    app.state.ssh = ssh
    app.state.node_core = node_core
    app.state.remote = remote
    app.state.remote_runs = remote_runs
    app.state.run_results = run_results
    app.state.file_runs = file_runs
    app.state.workspace_usage = workspace_usage
    app.state.auth = auth
    app.state.runtime_stamp = runtime_stamp()
    app.mount(
        "/static",
        _StaticGZipMiddleware(_CacheAwareStaticFiles(directory=PACKAGE_ROOT / "static")),
        name="static",
    )
    app.router.add_event_handler("startup", remote_runs.startup)
    app.router.add_event_handler("startup", file_runs.startup)
    app.router.add_event_handler("shutdown", file_runs.shutdown)
    app.router.add_event_handler("shutdown", remote_runs.shutdown)
    app.router.add_event_handler("shutdown", ssh.close)
    app.router.add_event_handler("shutdown", node_core.shutdown)

    @app.middleware("http")
    async def reject_mixed_runtime(request: Request, call_next):  # type: ignore[no-untyped-def]
        if (
            request.url.path not in {"/health", "/sw.js"}
            and not request.url.path.startswith(("/static/", "/icons/"))
            and runtime_stamp() != app.state.runtime_stamp
        ):
            headers = {"X-Termroom-Restart-Required": "1"}
            if request.url.path.startswith("/api/") or "application/json" in request.headers.get(
                "accept", ""
            ):
                return JSONResponse(
                    {
                        "ok": False,
                        "code": "restart_required",
                        "error": translate(
                            locale_from_request(request), "app.restart_required"
                        ),
                    },
                    status_code=503,
                    headers=headers,
                )
            return HTMLResponse(
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<meta name='color-scheme' content='dark light'>"
                "<script>(()=>{let t;try{t=localStorage.getItem('termroom.theme')}catch{};"
                "if(t!=='dark'&&t!=='light')t=matchMedia('(prefers-color-scheme:light)').matches?"
                "'light':'dark';document.documentElement.dataset.theme=t})()</script>"
                "<style>:root{color-scheme:dark;--bg:#212830;--surface:#2a313c;"
                "--border:#3d444d;--text:#e1e6ed;--muted:#b7bec8;--accent:#adbbff}"
                ":root[data-theme=light]{color-scheme:light;--bg:#d9d6ce;"
                "--surface:#ece9e1;--border:#afa99f;--text:#302f2c;"
                "--muted:#514f4a;--accent:#4a5880}*{box-sizing:border-box}"
                "body{margin:0;background:var(--bg);color:var(--text);"
                "font-family:Inter,ui-sans-serif,system-ui,sans-serif;line-height:1.6}"
                "main{max-width:680px;margin:12vh auto;padding:28px;border:1px solid var(--border);"
                "border-radius:10px;background:var(--surface)}h1{margin:0 0 12px;font-size:1.65rem}"
                "p{margin:0;color:var(--muted)}code{color:var(--accent)}</style>"
                "<title>Termroom update</title></head><body><main>"
                "<h1>Termroom was updated</h1>"
                "<p>The running Core is using older code. Run <code>termroom .</code> "
                "once in a terminal to restart the Core, then refresh this page.</p>"
                "</main></body></html>",
                status_code=503,
                headers=headers,
            )
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        path = request.url.path
        if path.startswith("/static/"):
            if response.status_code not in {200, 206, 304}:
                response.headers["Cache-Control"] = "no-store"
            elif path in {"/static/manifest.webmanifest", "/static/sw.js"}:
                response.headers["Cache-Control"] = "no-cache"
            elif request.query_params.get("v"):
                response.headers["Cache-Control"] = (
                    "public, max-age=31536000, immutable"
                )
            else:
                response.headers["Cache-Control"] = "no-cache"
        elif path != "/health":
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    def is_remote(workspace: Mapping[str, Any]) -> bool:
        return workspace.get("backend_kind") == "remote"

    def require_local_workspaces() -> None:
        if not settings.allow_local_workspaces:
            raise HTTPException(status_code=404, detail="Local Workspaces are disabled")

    def ensure_exposed_local_path(
        workspace: Mapping[str, Any],
        relative_path: str,
        *,
        must_exist: bool = True,
    ) -> None:
        if is_remote(workspace):
            return
        workspace_root = Path(workspace["path"]).resolve(strict=True)
        try:
            config_root = settings.state_dir.resolve(strict=True)
        except OSError:
            config_root = settings.state_dir.resolve(strict=False)
        if not is_within(config_root, workspace_root):
            return
        raw = Path(relative_path)
        if not raw.is_absolute():
            lexical_target = Path(os.path.normpath(str(workspace_root / raw)))
            if lexical_target == config_root or is_within(lexical_target, config_root):
                raise PathBoundaryError("Termroom internal config is not exposed through Files")
        target = resolve_inside(workspace_root, relative_path, must_exist=must_exist)
        if target == config_root or is_within(target, config_root):
            raise PathBoundaryError("Termroom internal config is not exposed through Files")

    async def ensure_terminal_list(workspace: dict[str, Any]) -> list[dict[str, Any]]:
        if is_remote(workspace):
            return await remote.ensure_workspace(workspace)
        return terminals.ensure_workspace(workspace)

    async def list_workspace_dir(
        workspace: dict[str, Any], relative_path: str
    ) -> tuple[Any, list[Any]]:
        if is_remote(workspace):
            return await remote.list_dir(workspace, relative_path)
        ensure_exposed_local_path(workspace, relative_path)
        return files.list_dir(workspace["path"], relative_path)

    async def stat_workspace_file(workspace: dict[str, Any], relative_path: str):  # type: ignore[no-untyped-def]
        if is_remote(workspace):
            return await remote.stat(workspace, relative_path)
        ensure_exposed_local_path(workspace, relative_path)
        return files.stat(workspace["path"], relative_path)

    async def open_workspace_terminal_editor(
        workspace: dict[str, Any], relative_path: str
    ) -> dict[str, Any]:
        if is_remote(workspace):
            return await remote.open_terminal_editor(workspace, relative_path)
        return await asyncio.to_thread(
            terminals.open_terminal_editor, workspace, relative_path
        )

    async def read_workspace_text(workspace: dict[str, Any], relative_path: str):  # type: ignore[no-untyped-def]
        if is_remote(workspace):
            return await remote.read_text(workspace, relative_path, settings.max_edit_bytes)
        ensure_exposed_local_path(workspace, relative_path)
        return files.read_text(workspace["path"], relative_path)

    async def write_workspace_text(
        workspace: dict[str, Any],
        relative_path: str,
        content: str,
        *,
        expected_digest: str,
        expected_mtime_ns: int,
    ) -> FileSnapshot:
        if is_remote(workspace):
            return await remote.write_text(
                workspace,
                relative_path,
                content,
                expected_digest=expected_digest,
                expected_mtime_ns=expected_mtime_ns,
                max_bytes=settings.max_edit_bytes,
            )
        ensure_exposed_local_path(workspace, relative_path)
        return files.write_text(
            workspace["path"],
            relative_path,
            content,
            expected_digest=expected_digest,
            expected_mtime_ns=expected_mtime_ns,
        )

    async def read_workspace_preview(
        workspace: dict[str, Any], relative_path: str, mode: str, offset: int = 0
    ):  # type: ignore[no-untyped-def]
        if is_remote(workspace):
            return await remote.read_text_preview(
                workspace,
                relative_path,
                mode=mode,
                offset=offset,
                max_bytes=settings.max_preview_bytes,
            )
        ensure_exposed_local_path(workspace, relative_path)
        return files.read_text_preview(
            workspace["path"],
            relative_path,
            mode=mode,
            offset=offset,
            max_bytes=settings.max_preview_bytes,
        )

    async def recent_workspace_files(workspace: dict[str, Any]):  # type: ignore[no-untyped-def]
        if is_remote(workspace):
            return await remote.recent_files(workspace)
        return await asyncio.to_thread(files.recent_files, workspace["path"])

    async def managed_key_display(locale: str) -> tuple[str, str | None]:
        try:
            managed_key = await asyncio.to_thread(ssh.ensure_managed_key)
        except SSHBackendError as exc:
            return "", _localized_exception(locale, exc)
        return managed_key["public_key"], None

    def workspace_content_type(workspace: dict[str, Any], relative_path: str) -> str:
        if is_remote(workspace):
            return remote.content_type(relative_path)
        return files.content_type(relative_path)

    async def build_workspace_archive(
        workspace: dict[str, Any],
        parent: str,
        selected_paths: list[str],
    ) -> Path:
        if not selected_paths or len(selected_paths) > FILE_BROWSER_PAGE_SIZE:
            raise ValueError("Select between 1 and 200 items")

        directory, all_entries = await list_workspace_dir(workspace, parent)
        relative_parent = (
            _normalize_relative_path(parent)
            if is_remote(workspace)
            else _workspace_relative(workspace["path"], directory)
        )
        visible_entries = {
            entry.relative_path: entry
            for entry in all_entries
            if not _is_internal_state_entry(
                settings, workspace, relative_parent, entry.name
            )
        }
        selected_entries = []
        seen: set[str] = set()
        for raw_path in selected_paths:
            path = _normalize_relative_path(raw_path)
            if path in seen:
                continue
            entry = visible_entries.get(path)
            if entry is None:
                raise ValueError("The selected item is no longer in this folder")
            selected_entries.append(entry)
            seen.add(path)
        if not selected_entries:
            raise ValueError("Select at least one file or folder")

        descriptor, archive_path = tempfile.mkstemp(prefix="termroom-download-", suffix=".zip")
        os.close(descriptor)

        def write_local_archive() -> None:
            root = Path(workspace["path"]).resolve(strict=True)
            added = 0

            def add_path(zf: zipfile.ZipFile, relative_path: str, archive_name: str) -> None:
                nonlocal added
                if added >= 10_000:
                    raise ValueError("Archive contains too many files")
                ensure_exposed_local_path(workspace, relative_path)
                target = resolve_inside(root, relative_path)
                if target.is_symlink():
                    return
                if target.is_dir():
                    zf.writestr(archive_name.rstrip("/") + "/", b"")
                    for child in sorted(target.iterdir(), key=lambda item: item.name.casefold()):
                        if child.is_symlink():
                            continue
                        child_relative = child.relative_to(root).as_posix()
                        if _is_internal_state_entry(
                            settings,
                            workspace,
                            _workspace_relative(root, target),
                            child.name,
                        ):
                            continue
                        add_path(
                            zf,
                            child_relative,
                            f"{archive_name.rstrip('/')}/{child.name}",
                        )
                    return
                if not target.is_file():
                    return
                zf.write(target, archive_name)
                added += 1

            with zipfile.ZipFile(
                archive_path,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                allowZip64=True,
            ) as zf:
                for entry in selected_entries:
                    add_path(zf, entry.relative_path, entry.name)

        async def write_remote_archive() -> None:
            added = 0
            with zipfile.ZipFile(
                archive_path,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                allowZip64=True,
            ) as zf:
                stack = [
                    (entry.relative_path, entry.name, entry.is_dir)
                    for entry in reversed(selected_entries)
                ]
                while stack:
                    relative_path, archive_name, directory_entry = stack.pop()
                    if directory_entry:
                        zf.writestr(archive_name.rstrip("/") + "/", b"")
                        _, children = await remote.list_dir(workspace, relative_path)
                        for child in reversed(children):
                            stack.append(
                                (
                                    child.relative_path,
                                    f"{archive_name.rstrip('/')}/{child.name}",
                                    child.is_dir,
                                )
                            )
                        continue
                    if added >= 10_000:
                        raise ValueError("Archive contains too many files")
                    with zf.open(archive_name, "w", force_zip64=True) as output:
                        async for chunk in remote.download_stream(
                            workspace, relative_path
                        ):
                            await asyncio.to_thread(output.write, chunk)
                    added += 1

        try:
            if is_remote(workspace):
                await write_remote_archive()
            else:
                await asyncio.to_thread(write_local_archive)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(archive_path)
            raise
        return Path(archive_path)

    @app.middleware("http")
    async def authenticate(request: Request, call_next):  # type: ignore[no-untyped-def]
        if (
            request.url.path in {"/health", "/login", "/sw.js"}
            or request.url.path.startswith("/api/node/enroll")
            or request.url.path.startswith("/static/")
            or request.url.path.startswith("/icons/")
            or request.url.path.startswith("/locale/")
        ):
            return await call_next(request)

        session = auth.request_session(request)
        if not session:
            locale = locale_from_request(request)
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context=_context(
                    settings,
                    title=translate(locale, "title.login"),
                    error=None,
                ),
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
        request.state.session = session
        return await call_next(request)

    @app.middleware("http")
    async def restore_https_proxy_scheme(request: Request, call_next):  # type: ignore[no-untyped-def]
        if settings.secure_cookie:
            forwarded_proto = request.headers.get("x-forwarded-proto", "")
            if forwarded_proto.partition(",")[0].strip().lower() == "https":
                request.scope["scheme"] = "https"
        return await call_next(request)

    @app.exception_handler(PathBoundaryError)
    async def path_error(request: Request, exc: PathBoundaryError) -> HTMLResponse:
        return _error_page(
            request,
            _localized_exception(locale_from_request(request), exc),
            403,
        )

    @app.exception_handler(UnsupportedFileError)
    async def unsupported_file(request: Request, exc: UnsupportedFileError) -> HTMLResponse:
        return _error_page(
            request,
            _localized_exception(locale_from_request(request), exc),
            400,
        )

    @app.exception_handler(SSHBackendError)
    async def ssh_error(request: Request, exc: SSHBackendError) -> HTMLResponse:
        return _error_page(
            request,
            _localized_exception(locale_from_request(request), exc),
            502,
        )

    @app.exception_handler(RemoteAccessError)
    async def remote_access_error(
        request: Request, exc: RemoteAccessError
    ) -> HTMLResponse:
        return _error_page(
            request,
            _localized_exception(locale_from_request(request), exc),
            502,
        )

    @app.exception_handler(FileNotFoundError)
    async def missing_path(request: Request, exc: FileNotFoundError) -> HTMLResponse:
        return _error_page(
            request,
            _localized_exception(locale_from_request(request), exc),
            404,
        )

    @app.exception_handler(NotADirectoryError)
    async def not_a_directory(request: Request, exc: NotADirectoryError) -> HTMLResponse:
        return _error_page(
            request,
            _localized_exception(locale_from_request(request), exc),
            400,
        )

    @app.exception_handler(PermissionError)
    async def permission_denied(request: Request, exc: PermissionError) -> HTMLResponse:
        return _error_page(
            request,
            _localized_exception(locale_from_request(request), exc),
            403,
        )

    @app.exception_handler(HTTPException)
    async def friendly_http_error(request: Request, exc: HTTPException):  # type: ignore[no-untyped-def]
        accept = request.headers.get("accept", "")
        if "text/html" not in accept:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        locale = locale_from_request(request)
        if exc.status_code == 404:
            message = translate(locale, "error.http.not_found")
        elif exc.status_code == 403:
            message = translate(locale, "error.http.forbidden")
        else:
            message = str(exc.detail or translate(locale, "error.http.generic"))
        return _error_page(request, message, exc.status_code)

    @app.get("/health", response_class=PlainTextResponse)
    async def health() -> str:
        return "ok"

    @app.get("/sw.js")
    async def service_worker():  # type: ignore[no-untyped-def]
        response = FileResponse(PACKAGE_ROOT / "static" / "sw.js", media_type="text/javascript")
        response.headers["Service-Worker-Allowed"] = "/"
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/icons/termroom-{size}.png")
    async def pwa_icon(request: Request, size: int) -> Response:
        try:
            content = await asyncio.to_thread(termroom_png_icon, size)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Unsupported icon size") from exc
        etag = f'"{hashlib.sha256(content).hexdigest()}"'
        cache_control = (
            "public, max-age=31536000, immutable"
            if request.query_params.get("v") == PWA_ICON_VERSION
            else "no-cache"
        )
        response_headers = {"Cache-Control": cache_control, "ETag": etag}
        if_none_match = request.headers.get("if-none-match", "")
        if any(
            candidate.strip().removeprefix("W/") in {"*", etag}
            for candidate in if_none_match.split(",")
        ):
            return Response(status_code=304, headers=response_headers)
        return Response(
            content=content,
            media_type="image/png",
            headers=response_headers,
        )

    @app.get("/locale/{locale}")
    async def set_locale(request: Request, locale: str, next: str = "/"):  # type: ignore[no-untyped-def]
        requested = locale.lower().split("-", 1)[0]
        if requested not in SUPPORTED_LOCALES:
            raise HTTPException(status_code=404, detail="Unsupported locale")
        next_parts = urlparse(next)
        destination = (
            next
            if next.startswith("/")
            and not next.startswith("//")
            and "\\" not in next
            and not next_parts.scheme
            and not next_parts.netloc
            else "/"
        )
        response = RedirectResponse(destination, status_code=303)
        response.set_cookie(
            LOCALE_COOKIE,
            normalize_locale(locale),
            httponly=False,
            secure=settings.secure_cookie,
            samesite="lax",
            max_age=60 * 60 * 24 * 365,
        )
        return response

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> HTMLResponse:
        locale = locale_from_request(request)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context=_context(
                settings,
                title=translate(locale, "title.login"),
                error=None,
            ),
        )

    @app.post("/login", response_class=HTMLResponse)
    async def login(request: Request):  # type: ignore[no-untyped-def]
        locale = locale_from_request(request)
        form = await request.form()
        supplied = str(form.get("password", ""))
        remote_key = request.client.host if request.client else "unknown"
        try:
            token = auth.login(supplied, remote_key=remote_key)
        except AuthRateLimited:
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context=_context(
                    settings,
                    title=translate(locale, "title.login"),
                    error=translate(locale, "login.rate_limited"),
                ),
                status_code=429,
                headers={"Retry-After": "60"},
            )
        if not token:
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context=_context(
                    settings,
                    title=translate(locale, "title.login"),
                    error=translate(locale, "login.invalid"),
                ),
                status_code=401,
            )
        response = RedirectResponse("/", status_code=303)
        auth.set_session_cookie(response, token)
        return response

    @app.post("/logout")
    async def logout(request: Request):  # type: ignore[no-untyped-def]
        await _verified_form(request, settings)
        session_id = str(getattr(request.state, "session", {}).get("id", ""))
        for socket in active_websockets.pop(session_id, []):
            with contextlib.suppress(RuntimeError):
                await socket.close(code=4401)
        response = RedirectResponse("/login", status_code=303)
        auth.clear_session_cookie(response)
        return response

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> HTMLResponse:
        locale = locale_from_request(request)
        remote_runs.schedule_cleanup()
        recent = [
            workspace
            for workspace in workspaces.list_recent()
            if settings.allow_local_workspaces or is_remote(workspace)
        ]
        recent_runs = remote_runs.list_recent(limit=6)
        active_sessions = terminals.existing_sessions()
        for workspace in recent:
            workspace.update(
                _workspace_status(
                    store,
                    terminals,
                    workspace,
                    session_active=workspace["tmux_session"] in active_sessions,
                )
            )
            workspace["last_opened_label"] = _relative_time(
                workspace["last_opened_at"], locale
            )
        for run in recent_runs:
            target = run.get("target") or {}
            run["target_label"] = str(target.get("name") or "")
            run["created_label"] = _relative_time(str(run["created_at"]), locale)
            display_state = _run_display_state(run.get("state"), run.get("exit_code"))
            run["display_state"] = display_state
            run["state_label"] = translate(
                locale,
                f"remote_run.state.{display_state}",
            )
        return templates.TemplateResponse(
            request=request,
            name="home.html",
            context=_context(
                settings,
                title="Termroom",
                recent=recent,
                recent_runs=recent_runs,
            ),
        )

    @app.get("/activity", response_class=HTMLResponse)
    async def activity_page(
        request: Request,
        unavailable: bool = False,
    ) -> HTMLResponse:
        locale = locale_from_request(request)
        device_id = str(request.state.session["id"])
        events = [
            _activity_event_view(event, locale=locale)
            for event in store.list_activity_events(device_id=device_id)
        ]
        return templates.TemplateResponse(
            request=request,
            name="activity.html",
            context=_context(
                settings,
                title=translate(locale, "activity.heading"),
                events=events,
                unread_count=store.count_unread_events(device_id=device_id),
                unavailable=unavailable,
            ),
        )

    @app.post("/activity/read-all")
    async def activity_read_all(request: Request) -> RedirectResponse:
        await _verified_form(request, settings)
        store.mark_all_events_read(device_id=str(request.state.session["id"]))
        return RedirectResponse("/activity", status_code=303)

    @app.post("/activity/{event_id}/read")
    async def activity_read(
        request: Request,
        event_id: str,
    ) -> RedirectResponse:
        await _verified_form(request, settings)
        if store.mark_event_read(
            event_id, device_id=str(request.state.session["id"])
        ) is None:
            raise HTTPException(status_code=404, detail="Activity not found")
        return RedirectResponse("/activity", status_code=303)

    @app.post("/activity/{event_id}/open")
    async def activity_open(
        request: Request,
        event_id: str,
    ) -> RedirectResponse:
        form = await _verified_form(request, settings)
        event = store.mark_event_read(
            event_id, device_id=str(request.state.session["id"])
        )
        if event is None:
            raise HTTPException(status_code=404, detail="Activity not found")
        if event["subject_type"] == "remote_run" and event["subject_exists"]:
            return RedirectResponse(
                f"/remote-runs/{event['subject_id']}",
                status_code=303,
            )
        if event["subject_type"] == "file_run" and event["subject_exists"]:
            workspace_id = str(event.get("current_workspace_id") or "")
            relative_path = str(event.get("current_relative_path") or "")
            terminal_id = str(event.get("current_terminal_id") or "")
            if str(form.get("destination") or "") == "terminal" and terminal_id:
                return RedirectResponse(
                    _url_with_query(
                        f"/w/{workspace_id}/terminal", terminal=terminal_id
                    ),
                    status_code=303,
                )
            try:
                workspace = _require_workspace(workspaces, workspace_id)
                entry = await stat_workspace_file(workspace, relative_path)
                if entry.is_dir:
                    raise FileNotFoundError(relative_path)
            except (FileNotFoundError, KeyError, NotADirectoryError, UnsupportedFileError):
                return RedirectResponse("/activity?unavailable=1", status_code=303)
            except SSHBackendError:
                pass
            return RedirectResponse(
                _url_with_query(
                    f"/w/{workspace_id}/edit/{quote(relative_path, safe='/')}",
                    run=event["subject_id"],
                ),
                status_code=303,
            )
        return RedirectResponse("/activity?unavailable=1", status_code=303)

    @app.get("/api/activity/summary", response_class=JSONResponse)
    async def activity_summary(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "unread_count": store.count_unread_events(
                    device_id=str(request.state.session["id"])
                ),
            }
        )

    async def _refresh_terminal_activity_provider(
        provider_key: tuple[str, str],
        scoped_workspaces: list[dict[str, Any]],
    ) -> None:
        """Take one resilient snapshot from exactly one activity provider."""

        try:
            if provider_key[0] == "local":
                await asyncio.to_thread(terminals.refresh_activity, scoped_workspaces)
            else:
                await remote.refresh_terminal_activity(scoped_workspaces)
        except Exception:
            # Activity is an opportunistic enhancement. Cached state remains
            # usable when one Local or Remote provider is temporarily offline.
            return

    async def refresh_terminal_activity_provider(
        provider_key: tuple[str, str],
        scoped_workspaces: list[dict[str, Any]],
    ) -> None:
        """Serialize one provider and share any in-flight covered scope."""

        requested = {
            str(workspace["id"]): workspace for workspace in scoped_workspaces
        }
        while requested:
            in_flight = terminal_activity_refreshes.get(provider_key)
            if in_flight is not None:
                covered, task = in_flight
                await asyncio.shield(task)
                requested = {
                    workspace_id: workspace
                    for workspace_id, workspace in requested.items()
                    if workspace_id not in covered
                }
                continue

            covered = frozenset(requested)
            task = asyncio.create_task(
                _refresh_terminal_activity_provider(
                    provider_key, list(requested.values())
                )
            )
            terminal_activity_refreshes[provider_key] = (covered, task)

            def clear(
                completed: asyncio.Task[None],
                *,
                key: tuple[str, str] = provider_key,
            ) -> None:
                current = terminal_activity_refreshes.get(key)
                if current is not None and current[1] is completed:
                    terminal_activity_refreshes.pop(key, None)

            task.add_done_callback(clear)
            await asyncio.shield(task)
            return

    async def refresh_terminal_activity_scope(
        scoped_workspaces: list[dict[str, Any]],
    ) -> None:
        """Refresh Local once and each Remote computer once, concurrently."""

        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for workspace in scoped_workspaces:
            if workspace.get("backend_kind", "local") == "local":
                provider_key = ("local", "local")
            else:
                computer = workspace.get("computer")
                computer_id = workspace.get("computer_id")
                if not computer_id and isinstance(computer, dict):
                    computer_id = computer.get("id")
                if not computer_id:
                    continue
                provider_key = ("remote", str(computer_id))
            grouped.setdefault(provider_key, []).append(workspace)
        await asyncio.gather(
            *(
                refresh_terminal_activity_provider(provider_key, workspaces_for_provider)
                for provider_key, workspaces_for_provider in grouped.items()
            )
        )

    def terminal_activity_payload(
        request: Request,
        *,
        workspace_id: str | None = None,
        workspace_ids: list[str] | None = None,
        terminal_id: str | None = None,
    ) -> dict[str, Any]:
        summary = store.terminal_activity_summary(
            str(request.state.session["id"]),
            workspace_id=workspace_id,
            workspace_ids=workspace_ids,
            terminal_id=terminal_id,
        )
        return {"ok": True, **summary}

    @app.get("/api/terminal-activity/summary", response_class=JSONResponse)
    async def terminal_activity_summary(
        request: Request,
        workspace_id: Annotated[list[str] | None, Query()] = None,
    ) -> dict[str, Any]:
        requested_ids = list(dict.fromkeys(workspace_id or ()))
        if len(requested_ids) > 20:
            raise HTTPException(status_code=400, detail="Too many Workspaces requested")
        scoped_workspaces = store.terminal_activity_workspaces(requested_ids)
        if len(scoped_workspaces) != len(requested_ids):
            raise HTTPException(status_code=404, detail="Workspace not found")
        if not settings.allow_local_workspaces and any(
            not is_remote(workspace) for workspace in scoped_workspaces
        ):
            raise HTTPException(status_code=404, detail="Workspace not found")
        await refresh_terminal_activity_scope(scoped_workspaces)
        return terminal_activity_payload(
            request,
            workspace_ids=requested_ids,
        )

    @app.get(
        "/api/workspaces/{workspace_id}/terminal-activity",
        response_class=JSONResponse,
    )
    async def workspace_terminal_activity(
        request: Request, workspace_id: str
    ) -> dict[str, Any]:
        workspace = _require_workspace(workspaces, workspace_id)
        await refresh_terminal_activity_scope([workspace])
        return terminal_activity_payload(request, workspace_id=workspace_id)

    @app.post(
        "/api/activity/notifications/claim",
        response_class=JSONResponse,
    )
    async def claim_activity_notifications(request: Request) -> JSONResponse:
        _verified_csrf_header(request, settings)
        device_id = str(request.state.session["id"])
        locale = locale_from_request(request)
        events = [
            _activity_notification_payload(event, locale=locale)
            for event in store.claim_event_notifications(device_id)
        ]
        return JSONResponse(
            {
                "ok": True,
                "events": events,
                "unread_count": store.count_unread_events(device_id=device_id),
            }
        )

    @app.get("/remote-runs/new", response_class=HTMLResponse)
    async def remote_run_new_page(
        request: Request,
        source_kind: str = "workspace",
        source_workspace_id: str | None = None,
        source_path: str = ".",
        target_computer_id: str | None = None,
        retry_run_id: str | None = None,
    ) -> HTMLResponse:
        remote_runs.schedule_cleanup()
        retry_run = None
        if retry_run_id:
            retry_run = store.get_remote_run(retry_run_id)
            if (
                retry_run is None
                or str(retry_run.get("source_kind") or "") != "workspace"
                or not retry_run.get("source_workspace_id")
            ):
                raise HTTPException(status_code=404, detail="Retryable Remote Run not found")
            if str(retry_run.get("state") or "") in {"preparing", "running"}:
                raise HTTPException(status_code=409, detail="Remote Run is still active")
            source_kind = "workspace"
            source_workspace_id = str(retry_run["source_workspace_id"])
            source_path = str(retry_run.get("source_path") or ".")
            target_computer_id = str(retry_run.get("target_computer_id") or "")
        computers = [
            computer
            for computer in store.list_computers()
            if remote.supports_capability(computer, "remote_run")
        ]
        source_workspaces = [
            workspace
            for workspace in workspaces.list_all()
            if not workspace.get("transient")
            and (
                not remote.is_node(workspace)
                or remote.supports_capability(workspace, "remote_run_source")
            )
        ]
        source_workspace_by_id = {
            str(workspace["id"]): workspace for workspace in source_workspaces
        }
        source_workspace = source_workspace_by_id.get(source_workspace_id or "")
        if retry_run is not None and source_workspace is None:
            raise HTTPException(
                status_code=409,
                detail="Remote Run source Workspace is no longer available",
            )
        normalized_source_path = "."
        if source_workspace is not None:
            with contextlib.suppress(ValueError):
                normalized_source_path = normalize_source_relative_path(
                    source_path or ".",
                    allow_root=True,
                )
        requested_source_kind = "archive" if source_kind == "zip" else source_kind
        selected_source_kind = (
            "workspace"
            if source_workspace is not None
            else requested_source_kind
            if requested_source_kind in {"workspace", "git", "archive"}
            else "workspace"
        )
        computer_ids = {str(computer["id"]) for computer in computers}
        selected_target_id = target_computer_id if target_computer_id in computer_ids else ""
        source_path_display = ""
        if source_workspace is not None:
            source_path_display = str(source_workspace["canonical_path"])
            if normalized_source_path != ".":
                source_path_display = f"{source_path_display.rstrip('/')}/{normalized_source_path}"
        change_source_url = _url_with_query(
            "/remote-runs/new",
            target_computer_id=selected_target_id or None,
        )
        return_url = "/open"
        if selected_target_id:
            return_url = f"/open/{selected_target_id}"
        if source_workspace is not None:
            return_url = _url_with_query(
                f"/w/{source_workspace['id']}/files",
                path=normalized_source_path,
            )
        return templates.TemplateResponse(
            request=request,
            name="remote_run_new.html",
            context=_context(
                settings,
                title=translate(locale_from_request(request), "remote_run.new_heading"),
                computers=computers,
                source_workspaces=source_workspaces,
                source_workspace=source_workspace,
                selected_source_kind=selected_source_kind,
                selected_source_path=normalized_source_path,
                source_path_display=source_path_display,
                selected_target_id=selected_target_id,
                selected_command=str(retry_run.get("command") or "")
                if retry_run is not None
                else "",
                retry_run=retry_run,
                change_source_url=change_source_url,
                return_url=return_url,
                max_archive_bytes=settings.max_upload_bytes,
            ),
        )

    @app.get("/remote-runs/{run_id}/source")
    async def open_remote_run_source(run_id: str) -> RedirectResponse:
        run = store.get_remote_run(run_id)
        if (
            run is None
            or str(run.get("source_kind") or "") != "workspace"
            or not run.get("source_workspace_id")
        ):
            raise HTTPException(status_code=404, detail="Remote Run source not found")
        try:
            source_workspace = workspaces.require(str(run["source_workspace_id"]))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Source Workspace not found") from exc
        if source_workspace.get("transient"):
            raise HTTPException(status_code=404, detail="Source Workspace not found")
        try:
            source_path = normalize_source_relative_path(
                str(run.get("source_path") or "."), allow_root=True
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Remote Run source not found") from exc
        return RedirectResponse(
            _url_with_query(
                f"/w/{source_workspace['id']}/files",
                path=source_path,
            ),
            status_code=302,
        )

    @app.get("/remote-runs/{run_id}/result.zip")
    async def download_remote_run_result(
        request: Request, run_id: str
    ) -> FileResponse:
        locale = locale_from_request(request)
        try:
            archive_path = await run_results.create_archive(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Remote Run not found") from exc
        except ResultCollectionError as exc:
            raise HTTPException(
                status_code=409,
                detail=_localized_result_collection_exception(locale, exc),
            ) from exc
        except (OSError, RemoteRunError, RemoteAccessError, SSHBackendError) as exc:
            raise HTTPException(
                status_code=502,
                detail=translate(locale, "remote_run.collect.error.unavailable"),
            ) from exc
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=f"termroom-{run_id}-result.zip",
            background=BackgroundTask(os.unlink, archive_path),
        )

    @app.get("/remote-runs/{run_id}/collect", response_class=HTMLResponse)
    async def review_remote_run_collection(
        request: Request, run_id: str
    ) -> HTMLResponse:
        locale = locale_from_request(request)
        try:
            run = await asyncio.to_thread(remote_runs.get, run_id)
            plan = await run_results.review(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Remote Run not found") from exc
        except ResultCollectionError as exc:
            return _error_page(
                request,
                _localized_result_collection_exception(locale, exc),
                409,
            )
        except (OSError, RemoteRunError, RemoteAccessError, SSHBackendError):
            return _error_page(
                request,
                translate(locale, "remote_run.collect.error.unavailable"),
                502,
            )
        return templates.TemplateResponse(
            request=request,
            name="remote_run_collect.html",
            context=_context(
                settings,
                title=translate(locale, "remote_run.collect.heading"),
                run=run,
                plan=_remote_run_collection_view(plan, locale),
                report=None,
                collection_result=_remote_run_collection_result_query(request),
                action_error=None,
            ),
        )

    @app.post("/remote-runs/{run_id}/collect", response_class=HTMLResponse)
    async def apply_remote_run_collection(
        request: Request, run_id: str
    ) -> Response:
        locale = locale_from_request(request)
        form = await _verified_form(request, settings)
        revision = str(form.get("revision") or "")
        try:
            run = await asyncio.to_thread(remote_runs.get, run_id)
            report = await run_results.apply(run_id, revision)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Remote Run not found") from exc
        except ResultCollectionConflict as exc:
            try:
                run = await asyncio.to_thread(remote_runs.get, run_id)
                plan = await run_results.review(run_id)
            except (
                OSError,
                ResultCollectionError,
                RemoteRunError,
                RemoteAccessError,
                SSHBackendError,
            ):
                return _error_page(
                    request,
                    _localized_result_collection_exception(locale, exc),
                    409,
                )
            return templates.TemplateResponse(
                request=request,
                name="remote_run_collect.html",
                context=_context(
                    settings,
                    title=translate(locale, "remote_run.collect.heading"),
                    run=run,
                    plan=_remote_run_collection_view(plan, locale),
                    report=None,
                    action_error=_localized_result_collection_exception(locale, exc),
                ),
                status_code=409,
            )
        except ResultCollectionError as exc:
            return _error_page(
                request,
                _localized_result_collection_exception(locale, exc),
                409,
            )
        except (OSError, RemoteRunError, RemoteAccessError, SSHBackendError):
            return _error_page(
                request,
                translate(locale, "remote_run.collect.error.unavailable"),
                502,
            )
        summary = report.as_dict()["summary"]
        return RedirectResponse(
            _url_with_query(
                f"/remote-runs/{run_id}/collect",
                collected=1,
                applied=summary.get("applied", 0),
                conflict=summary.get("conflict", 0),
                already_result=summary.get("already_result", 0),
                skipped=summary.get("skipped", 0),
                failed=summary.get("failed", 0),
            ),
            status_code=303,
        )

    @app.post("/api/remote-runs", response_class=JSONResponse)
    async def create_remote_run(request: Request) -> JSONResponse:
        locale = locale_from_request(request)
        _verified_csrf_header(request, settings)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("Remote Run request must be an object")
            run, created = await remote_runs.create(payload)
        except (
            KeyError,
            OSError,
            ValueError,
            RemoteRunError,
            SSHBackendError,
            json.JSONDecodeError,
        ) as exc:
            return JSONResponse(
                {"ok": False, "error": _localized_remote_run_exception(locale, exc)},
                status_code=409 if isinstance(exc, RemoteRunConflict) else 400,
            )
        run_id = str(run["id"])
        return JSONResponse(
            {
                "ok": True,
                "created": created,
                "run_id": run_id,
                "detail_url": f"/remote-runs/{run_id}",
                "archive_url": f"/api/remote-runs/{run_id}/archive"
                if run["source_kind"] == "archive"
                else None,
            },
            status_code=202 if created else 200,
        )

    @app.post(
        "/api/remote-runs/{run_id}/archive",
        response_class=JSONResponse,
    )
    async def upload_remote_run_archive(
        request: Request,
        run_id: str,
        filename: str = "",
    ) -> JSONResponse:
        locale = locale_from_request(request)
        _verified_csrf_header(request, settings)
        try:
            header = request.headers.get("content-length")
            content_length = int(header) if header else None
            if content_length is not None and content_length < 0:
                raise ValueError("Invalid upload length")
            run = await remote_runs.upload_archive(
                run_id,
                filename,
                request.stream(),
                content_length=content_length,
            )
        except (
            KeyError,
            OSError,
            ValueError,
            RemoteRunError,
            SSHBackendError,
        ) as exc:
            return JSONResponse(
                {"ok": False, "error": _localized_remote_run_exception(locale, exc)},
                status_code=409
                if isinstance(exc, RemoteRunConflict)
                else _upload_error_status(exc),
            )
        return JSONResponse(
            {
                "ok": True,
                "run_id": str(run["id"]),
                "detail_url": f"/remote-runs/{run['id']}",
            },
            status_code=202,
        )

    @app.get("/remote-runs/{run_id}", response_class=HTMLResponse)
    async def remote_run_page(
        request: Request,
        run_id: str,
        error: str | None = None,
    ) -> Response:
        locale = locale_from_request(request)
        try:
            run = await asyncio.to_thread(remote_runs.get, run_id)
            if run["state"] in {"preparing", "running"}:
                with contextlib.suppress(OSError, RemoteRunError, SSHBackendError):
                    run = await asyncio.to_thread(
                        remote_runs.poll,
                        run_id,
                        offset=0,
                        limit=1,
                    )
            if (
                run["state"] in {"finished", "stopped", "lost"}
                and not run.get("workspace_id")
            ):
                with contextlib.suppress(
                    FileNotFoundError,
                    OSError,
                    RemoteRunError,
                    SSHBackendError,
                ):
                    run = await asyncio.to_thread(
                        remote_runs.ensure_workspace_bridge,
                        run_id,
                    )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Remote Run not found") from exc
        workspace_id = str(run.get("workspace_id") or "")
        if workspace_id:
            destination = f"/w/{workspace_id}/terminal"
            if error:
                destination = _url_with_query(destination, error=error)
            return RedirectResponse(
                destination,
                status_code=303,
            )
        target = run.get("target") or {}
        run["error_detail"] = _localized_remote_run_error_detail(locale, run)
        return templates.TemplateResponse(
            request=request,
            name="remote_run_wait.html",
            context=_context(
                settings,
                title=str(run["source_label"]),
                run=run,
                target_label=str(target.get("name") or ""),
                action_error=error,
                remote_run_results_available=run["state"] in TERMINAL_STATES,
                remote_run_collect_available=(
                    run["state"] in TERMINAL_STATES
                    and str(run.get("source_kind") or "") == "workspace"
                    and bool(run.get("source_workspace_id"))
                ),
            ),
        )

    @app.get(
        "/api/remote-runs/{run_id}/status",
        response_class=JSONResponse,
    )
    async def remote_run_status(request: Request, run_id: str) -> JSONResponse:
        locale = locale_from_request(request)
        try:
            run = await asyncio.to_thread(remote_runs.get, run_id)
            if run["state"] in {"preparing", "running"}:
                run = await asyncio.to_thread(
                    remote_runs.poll,
                    run_id,
                    offset=0,
                    limit=1,
                )
            if (
                run["state"] in {"finished", "stopped", "lost"}
                and not run.get("workspace_id")
            ):
                run = await asyncio.to_thread(
                    remote_runs.ensure_workspace_bridge,
                    run_id,
                )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Remote Run not found") from exc
        except (OSError, RemoteRunError, SSHBackendError) as exc:
            return JSONResponse(
                {"ok": False, "error": _localized_remote_run_exception(locale, exc)},
                status_code=502,
            )
        return JSONResponse(
            {"ok": True, **_remote_run_status_payload(run, locale=locale)}
        )

    @app.post("/remote-runs/{run_id}/stop")
    async def stop_remote_run(
        request: Request, run_id: str
    ) -> RedirectResponse:
        locale = locale_from_request(request)
        await _verified_form(request, settings)
        try:
            await asyncio.to_thread(remote_runs.stop, run_id)
        except (KeyError, OSError, ValueError, RemoteRunError, SSHBackendError) as exc:
            return RedirectResponse(
                _url_with_query(
                    f"/remote-runs/{run_id}",
                    error=_localized_exception(locale, exc),
                ),
                status_code=303,
            )
        return RedirectResponse(f"/remote-runs/{run_id}", status_code=303)

    @app.post("/remote-runs/{run_id}/kill")
    async def kill_remote_run(
        request: Request, run_id: str
    ) -> RedirectResponse:
        locale = locale_from_request(request)
        await _verified_form(request, settings)
        try:
            await asyncio.to_thread(remote_runs.kill, run_id)
        except (KeyError, OSError, ValueError, RemoteRunError, SSHBackendError) as exc:
            return RedirectResponse(
                _url_with_query(
                    f"/remote-runs/{run_id}",
                    error=_localized_exception(locale, exc),
                ),
                status_code=303,
            )
        return RedirectResponse(f"/remote-runs/{run_id}", status_code=303)

    @app.post("/remote-runs/{run_id}/delete")
    async def delete_remote_run(
        request: Request, run_id: str
    ) -> RedirectResponse:
        locale = locale_from_request(request)
        await _verified_form(request, settings)
        try:
            result = await asyncio.to_thread(remote_runs.request_delete, run_id)
        except (KeyError, OSError, ValueError, RemoteRunError, SSHBackendError) as exc:
            return RedirectResponse(
                _url_with_query(
                    f"/remote-runs/{run_id}",
                    error=_localized_exception(locale, exc),
                ),
                status_code=303,
            )
        if not result.get("deleted"):
            target = result.get("target") or {}
            return RedirectResponse(
                _url_with_query(
                    f"/remote-runs/{run_id}",
                    error=translate(
                        locale,
                        "remote_run.cleanup_pending_copy",
                        computer=str(target.get("name") or "SSH"),
                    ),
                ),
                status_code=303,
            )
        return RedirectResponse("/", status_code=303)

    @app.get("/open", response_class=HTMLResponse)
    async def workspace_open_hub(
        request: Request, computer_removed: bool = False
    ) -> HTMLResponse:
        locale = locale_from_request(request)
        computers = store.list_computers()
        local_roots = store.list_local_roots()
        root_workspace_counts, workspace_counts = store.workspace_location_counts()
        return templates.TemplateResponse(
            request=request,
            name="workspace_open.html",
            context=_context(
                settings,
                title=translate(locale_from_request(request), "open.heading"),
                mode="computers",
                computers=computers,
                computer_removed=computer_removed,
                local_root_count=len(local_roots),
                local_workspace_count=sum(root_workspace_counts.values()),
                workspace_counts=workspace_counts,
                allow_local_workspaces=settings.allow_local_workspaces,
                remote_statuses={
                    str(computer["id"]): _remote_connection_view(
                        computer, remote=remote, locale=locale
                    )
                    for computer in computers
                },
            ),
        )

    @app.get("/open/local", response_class=HTMLResponse)
    async def workspace_open_local(
        request: Request,
        root: str | None = None,
        path: str = ".",
        hidden: bool = False,
        browse_location: bool = False,
        location_path: str | None = None,
        location_hidden: bool = False,
        error: str | None = None,
        project_error: str | None = None,
        project_existing: str | None = None,
        project_created: str | None = None,
        project_workspace: str | None = None,
        project_name: str | None = None,
        location_removed: bool = False,
    ) -> HTMLResponse:
        require_local_workspaces()
        locale = locale_from_request(request)
        local_roots = store.list_local_roots()
        root_by_id = {str(item["id"]): item for item in local_roots}
        selected_root = root_by_id.get(root or "") or root_by_id.get(
            str(workspaces.root_record["id"])
        )
        if selected_root is None:
            selected_root = store.ensure_root(settings.root)
            local_roots = store.list_local_roots()
        selected_root_id = str(selected_root["id"])
        selected_root_path = Path(str(selected_root["path"]))
        root_unavailable = False
        try:
            root_manager = RootManager(selected_root_path)
            directory, all_entries = root_manager.list_directories(path)
            relative = root_manager.relative(directory)
        except OSError:
            root_unavailable = True
            directory = selected_root_path
            all_entries = []
            relative = "."
        hidden_count = sum(entry.name.startswith(".") for entry in all_entries)
        entries = (
            all_entries
            if hidden
            else [entry for entry in all_entries if not entry.name.startswith(".")]
        )
        location_close_url = _url_with_query(
            "/open/local",
            root=selected_root_id,
            path=relative,
            hidden=1 if hidden else None,
        )
        local_workspaces: list[dict[str, Any]] = []
        for item in store.list_workspaces_for_root(selected_root_id):
            try:
                hydrated = workspaces.require(str(item["id"]))
            except (KeyError, OSError):
                hydrated = {
                    **item,
                    "available": False,
                }
            else:
                hydrated["available"] = True
            local_workspaces.append(hydrated)
        root_workspace_counts, _computer_workspace_counts = (
            store.workspace_location_counts()
        )
        root_rows = []
        for item in local_roots:
            value = str(item["path"])
            try:
                available = Path(value).resolve(strict=True).is_dir()
            except OSError:
                available = False
            workspace_count = root_workspace_counts.get(str(item["id"]), 0)
            root_rows.append(
                {
                    **item,
                    "label": Path(value).name or value,
                    "workspace_count": workspace_count,
                    "available": available,
                    "removable": (
                        str(item["id"]) != str(workspaces.root_record["id"])
                        and workspace_count == 0
                    ),
                }
            )
        location_picker = None
        location_error = None
        if browse_location:
            try:
                location_picker = _local_location_picker(
                    location_path,
                    show_hidden=location_hidden,
                )
                picker_query = {
                    "root": selected_root_id,
                    "path": relative,
                    "hidden": 1 if hidden else None,
                    "browse_location": 1,
                    "location_hidden": 1 if location_hidden else None,
                }
                location_picker["parent_url"] = (
                    _url_with_query(
                        "/open/local",
                        **picker_query,
                        location_path=location_picker["parent"],
                    )
                    if location_picker["parent"] is not None
                    else None
                )
                for entry in location_picker["entries"]:
                    entry["url"] = _url_with_query(
                        "/open/local",
                        **picker_query,
                        location_path=entry["path"],
                    )
                location_picker["hidden_url"] = _url_with_query(
                    "/open/local",
                    root=selected_root_id,
                    path=relative,
                    hidden=1 if hidden else None,
                    browse_location=1,
                    location_path=location_picker["current"],
                    location_hidden=None if location_hidden else 1,
                )
            except (OSError, ValueError) as exc:
                location_error = _localized_exception(locale, exc)
        return templates.TemplateResponse(
            request=request,
            name="workspace_open.html",
            context=_context(
                settings,
                title=translate(locale, "open.local_heading"),
                mode="local",
                roots=root_rows,
                selected_root=selected_root,
                selected_root_id=selected_root_id,
                selected_root_label=(
                    Path(str(selected_root["path"])).name
                    or str(selected_root["path"])
                ),
                path=relative,
                current_path_display=str(directory),
                entries=entries,
                breadcrumbs=_breadcrumbs(relative),
                show_hidden=hidden,
                hidden_count=hidden_count,
                local_workspaces=local_workspaces,
                error=error,
                project_error=project_error,
                project_existing=project_existing,
                project_created=project_created,
                project_workspace=project_workspace,
                project_name=project_name or "",
                browse_location=browse_location,
                location_picker=location_picker,
                location_error=location_error,
                location_close_url=location_close_url,
                location_removed=location_removed,
                root_unavailable=root_unavailable,
                has_remote_computers=bool(store.list_computers()),
            ),
        )

    @app.post("/open/local/locations")
    async def add_local_location(request: Request):  # type: ignore[no-untyped-def]
        require_local_workspaces()
        locale = locale_from_request(request)
        form = await _verified_form(request, settings)
        raw_path = str(form.get("path", "")).strip()
        try:
            candidate = Path(raw_path).expanduser()
            if not candidate.is_absolute():
                raise ValueError(translate(locale, "open.location_absolute"))
            resolved = candidate.resolve(strict=True)
            if not resolved.is_dir():
                raise NotADirectoryError(resolved)
            root_record = store.ensure_root(resolved)
        except (OSError, ValueError) as exc:
            return RedirectResponse(
                _url_with_query(
                    "/open/local",
                    error=_localized_exception(locale, exc),
                ),
                status_code=303,
            )
        return RedirectResponse(
            _url_with_query("/open/local", root=root_record["id"]),
            status_code=303,
        )

    @app.post("/open/local/locations/{root_id}/remove")
    async def remove_local_location(
        request: Request, root_id: str
    ) -> RedirectResponse:
        require_local_workspaces()
        locale = locale_from_request(request)
        await _verified_form(request, settings)
        if root_id == str(workspaces.root_record["id"]):
            raise HTTPException(status_code=400, detail="Primary folder location is required")
        try:
            store.remove_local_root(root_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Folder location not found") from exc
        except RuntimeError:
            return RedirectResponse(
                _url_with_query(
                    "/open/local",
                    root=root_id,
                    error=translate(locale, "open.location_remove_workspaces_first"),
                ),
                status_code=303,
            )
        return RedirectResponse(
            _url_with_query("/open/local", location_removed=1), status_code=303
        )

    @app.post("/open/local/projects")
    async def create_local_project(request: Request):  # type: ignore[no-untyped-def]
        require_local_workspaces()
        locale = locale_from_request(request)
        form = await _verified_form(request, settings)
        root_id = str(form.get("root_id", "")).strip()
        parent = str(form.get("parent", "."))
        name = str(form.get("name", ""))
        root_record = store.get_root(root_id)
        if not root_record or str(root_record["path"]).startswith("ssh://"):
            raise HTTPException(status_code=404, detail="Local folder location not found")
        root_manager = RootManager(Path(str(root_record["path"])))
        try:
            workspace, created_path = workspaces.create_local_project(
                str(root_record["path"]), parent, name
            )
        except ProjectCreatedButWorkspaceFailed as exc:
            existing_relative = None
            with contextlib.suppress(OSError, ValueError):
                existing_relative = root_manager.relative(Path(exc.path))
            return RedirectResponse(
                _url_with_query(
                    "/open/local",
                    root=root_id,
                    path=parent,
                    project_error=translate(
                        locale,
                        "project.error.created_not_opened",
                        error=_localized_exception(locale, exc.cause),
                    ),
                    project_created=exc.path,
                    project_existing=existing_relative,
                    project_name=name,
                ),
                status_code=303,
            )
        except ProjectPathExists as exc:
            existing_relative = None
            if exc.is_directory:
                with contextlib.suppress(OSError, ValueError):
                    existing_relative = root_manager.relative(Path(exc.path))
            message = translate(
                locale,
                "project.error.folder_exists" if exc.is_directory else "project.error.file_exists",
                path=exc.path,
            )
            return RedirectResponse(
                _url_with_query(
                    "/open/local",
                    root=root_id,
                    path=parent,
                    project_error=message,
                    project_existing=existing_relative,
                    project_name=name,
                ),
                status_code=303,
            )
        except ProjectNameError as exc:
            return RedirectResponse(
                _url_with_query(
                    "/open/local",
                    root=root_id,
                    path=parent,
                    project_error=_localized_exception(locale, exc),
                    project_name=name,
                ),
                status_code=303,
            )
        except PermissionError:
            target = str(root_manager.resolve(parent) / name)
            return RedirectResponse(
                _url_with_query(
                    "/open/local",
                    root=root_id,
                    path=parent,
                    project_error=translate(
                        locale,
                        "project.error.permission",
                        computer=translate(locale, "home.this_computer"),
                        path=target,
                    ),
                    project_name=name,
                ),
                status_code=303,
            )
        except (OSError, ValueError) as exc:
            return RedirectResponse(
                _url_with_query(
                    "/open/local",
                    root=root_id,
                    path=parent,
                    project_error=_localized_exception(locale, exc),
                    project_name=name,
                ),
                status_code=303,
            )
        try:
            await asyncio.to_thread(terminals.ensure_workspace, workspace)
        except (OSError, TerminalError) as exc:
            return RedirectResponse(
                _url_with_query(
                    "/open/local",
                    root=root_id,
                    path=parent,
                    project_error=translate(
                        locale,
                        "project.error.created_not_opened",
                        error=_localized_exception(locale, exc),
                    ),
                    project_created=str(created_path),
                    project_workspace=workspace["id"],
                    project_name=name,
                ),
                status_code=303,
            )
        return RedirectResponse(f"/w/{workspace['id']}/terminal", status_code=303)

    @app.get("/api/local/browse-directories", response_class=JSONResponse)
    async def browse_local_directories(
        request: Request,
        path: str | None = None,
        hidden: bool = False,
    ) -> JSONResponse:
        require_local_workspaces()
        locale = locale_from_request(request)
        try:
            picker = _local_location_picker(path, show_hidden=hidden)
        except (OSError, ValueError) as exc:
            return JSONResponse(
                {"ok": False, "error": _localized_exception(locale, exc)},
                status_code=400,
            )
        return JSONResponse({"ok": True, **picker})

    @app.get("/open/{computer_id}", response_class=HTMLResponse)
    async def workspace_open_remote(
        request: Request,
        computer_id: str,
        browse: bool = False,
        browse_path: str | None = None,
        browse_hidden: bool = False,
        error: str | None = None,
        connected: bool = False,
        project_error: str | None = None,
        project_existing: str | None = None,
        project_created: str | None = None,
        project_workspace: str | None = None,
        project_name: str | None = None,
    ) -> HTMLResponse:
        locale = locale_from_request(request)
        computer = _require_computer(store, computer_id)
        remote_workspaces = []
        for item in store.list_workspaces_for_computer(computer_id):
            try:
                remote_workspaces.append(workspaces.require(str(item["id"])))
            except (KeyError, OSError):
                continue
        computer_runs = store.list_remote_runs_for_computer(computer_id)
        for run in computer_runs:
            run["created_label"] = _relative_time(str(run["created_at"]), locale)
            display_state = _run_display_state(run.get("state"), run.get("exit_code"))
            run["display_state"] = display_state
            run["state_label"] = translate(
                locale,
                f"remote_run.state.{display_state}",
            )
        remote_picker = None
        browse_error = None
        remote_browse_close_url = f"/open/{computer_id}"
        if browse:
            try:
                remote_picker = await remote.list_browse_directories(
                    computer,
                    browse_path,
                    show_hidden=browse_hidden,
                )
                picker_query = {
                    "browse": 1,
                    "browse_hidden": 1 if browse_hidden else None,
                }
                remote_picker["parent_url"] = (
                    _url_with_query(
                        f"/open/{computer_id}",
                        **picker_query,
                        browse_path=remote_picker["parent"],
                    )
                    if remote_picker["parent"] is not None
                    else None
                )
                for entry in remote_picker["entries"]:
                    entry["url"] = _url_with_query(
                        f"/open/{computer_id}",
                        **picker_query,
                        browse_path=entry["path"],
                    )
                remote_picker["hidden_url"] = _url_with_query(
                    f"/open/{computer_id}",
                    browse=1,
                    browse_path=remote_picker["current"],
                    browse_hidden=None if browse_hidden else 1,
                )
            except (OSError, ValueError, SSHBackendError, RemoteAccessError) as exc:
                browse_error = _localized_exception(locale, exc)
        return templates.TemplateResponse(
            request=request,
            name="workspace_open.html",
            context=_context(
                settings,
                title=str(computer["name"]),
                mode="remote",
                computer=computer,
                remote_status=_remote_connection_view(
                    computer, remote=remote, locale=locale
                ),
                can_remote_run=remote.supports_capability(computer, "remote_run"),
                remote_workspaces=remote_workspaces,
                remote_runs=computer_runs,
                error=error,
                project_error=project_error,
                project_existing=project_existing,
                project_created=project_created,
                project_workspace=project_workspace,
                project_name=project_name or "",
                connected=connected,
                browse=browse,
                browse_path=browse_path,
                remote_picker=remote_picker,
                browse_error=browse_error,
                remote_browse_close_url=remote_browse_close_url,
                has_multiple_computers=True,
            ),
        )

    @app.get(
        "/api/computers/{computer_id}/browse-directories",
        response_class=JSONResponse,
    )
    async def browse_remote_directories(
        request: Request,
        computer_id: str,
        path: str | None = None,
        hidden: bool = False,
    ) -> JSONResponse:
        locale = locale_from_request(request)
        computer = _require_computer(store, computer_id)
        try:
            picker = await remote.list_browse_directories(
                computer,
                path,
                show_hidden=hidden,
            )
        except (OSError, ValueError, SSHBackendError, RemoteAccessError) as exc:
            return JSONResponse(
                {"ok": False, "error": _localized_exception(locale, exc)},
                status_code=400,
            )
        return JSONResponse({"ok": True, **picker})

    @app.post("/computers/{computer_id}/projects")
    async def create_remote_project(
        request: Request, computer_id: str
    ):  # type: ignore[no-untyped-def]
        locale = locale_from_request(request)
        form = await _verified_form(request, settings)
        computer = _require_computer(store, computer_id)
        parent = str(form.get("parent", "")).strip()
        name = str(form.get("name", ""))
        target_hint = f"{parent.rstrip('/')}/{name}" if parent else name
        try:
            canonical = await remote.create_project_directory(computer, parent, name)
        except ProjectPathExists as exc:
            message = translate(
                locale,
                "project.error.folder_exists" if exc.is_directory else "project.error.file_exists",
                path=exc.path,
            )
            return RedirectResponse(
                _url_with_query(
                    f"/open/{computer_id}",
                    browse=1,
                    browse_path=parent,
                    project_error=message,
                    project_existing=exc.path if exc.is_directory else None,
                    project_name=name,
                ),
                status_code=303,
            )
        except ProjectNameError as exc:
            return RedirectResponse(
                _url_with_query(
                    f"/open/{computer_id}",
                    browse=1,
                    browse_path=parent,
                    project_error=_localized_exception(locale, exc),
                    project_name=name,
                ),
                status_code=303,
            )
        except PermissionError:
            return RedirectResponse(
                _url_with_query(
                    f"/open/{computer_id}",
                    browse=1,
                    browse_path=parent,
                    project_error=translate(
                        locale,
                        "project.error.permission",
                        computer=computer["name"],
                        path=target_hint,
                    ),
                    project_name=name,
                ),
                status_code=303,
            )
        except (OSError, ValueError, SSHBackendError, RemoteAccessError) as exc:
            return RedirectResponse(
                _url_with_query(
                    f"/open/{computer_id}",
                    browse=1,
                    browse_path=parent or None,
                    project_error=translate(
                        locale,
                        "project.error.remote_create",
                        computer=computer["name"],
                        error=_localized_exception(locale, exc),
                    ),
                    project_name=name,
                ),
                status_code=303,
            )

        try:
            workspace = workspaces.open_remote(computer_id, canonical)
        except (KeyError, OSError, RuntimeError, ValueError) as exc:
            return RedirectResponse(
                _url_with_query(
                    f"/open/{computer_id}",
                    browse=1,
                    browse_path=parent,
                    project_error=translate(
                        locale,
                        "project.error.created_not_opened",
                        error=_localized_exception(locale, exc),
                    ),
                    project_created=canonical,
                    project_existing=canonical,
                    project_name=name,
                ),
                status_code=303,
            )
        try:
            await remote.ensure_workspace(workspace)
        except (OSError, ValueError, SSHBackendError, RemoteAccessError) as exc:
            return RedirectResponse(
                _url_with_query(
                    f"/open/{computer_id}",
                    browse=1,
                    browse_path=parent,
                    project_error=translate(
                        locale,
                        "project.error.created_not_opened",
                        error=_localized_exception(locale, exc),
                    ),
                    project_created=canonical,
                    project_workspace=workspace["id"],
                    project_name=name,
                ),
                status_code=303,
            )
        return RedirectResponse(f"/w/{workspace['id']}/terminal", status_code=303)

    @app.get("/computers/node/pair", response_class=HTMLResponse)
    async def node_pair_page(
        request: Request, pairing_id: str | None = None
    ) -> HTMLResponse:
        pairing = store.get_node_pairing(pairing_id) if pairing_id else None
        if pairing_id and pairing is None:
            raise HTTPException(status_code=404, detail="Node pairing not found")
        return templates.TemplateResponse(
            request=request,
            name="node_pair.html",
            context=_context(
                settings,
                title=translate(locale_from_request(request), "node.pair.heading"),
                pairing=pairing,
                code=None,
                **_node_pairing_reachability(request),
                error=None,
            ),
        )

    @app.post("/computers/node/pair/check", response_class=HTMLResponse)
    async def check_node_pairing(request: Request) -> HTMLResponse:
        form = await _verified_form(request, settings)
        pairing_id = str(form.get("pairing_id", ""))
        code = str(form.get("code", ""))
        pairing = store.get_node_pairing(pairing_id)
        if pairing is None:
            raise HTTPException(status_code=404, detail="Node pairing not found")
        if not secure_compare(str(pairing["code_hash"]), pairing_code_digest(code)):
            raise HTTPException(status_code=409, detail="Node pairing code changed")
        return templates.TemplateResponse(
            request=request,
            name="node_pair.html",
            context=_context(
                settings,
                title=translate(locale_from_request(request), "node.pair.heading"),
                pairing=pairing,
                code=code if pairing.get("status") is None else None,
                **_node_pairing_reachability(request),
                error=None,
            ),
        )

    @app.post("/computers/node/pair", response_class=HTMLResponse)
    async def create_node_pairing(request: Request) -> HTMLResponse:
        await _verified_form(request, settings)
        code = generate_pairing_code()
        expires_at = (
            datetime.now(UTC) + timedelta(seconds=PAIRING_CODE_TTL_SECONDS)
        ).isoformat(timespec="seconds")
        pairing = store.create_node_pairing_code(
            code_hash=pairing_code_digest(code), expires_at=expires_at
        )
        return templates.TemplateResponse(
            request=request,
            name="node_pair.html",
            context=_context(
                settings,
                title=translate(locale_from_request(request), "node.pair.heading"),
                pairing=pairing,
                code=code,
                **_node_pairing_reachability(request),
                error=None,
            ),
            status_code=201,
        )

    @app.post("/computers/node/pair/{enrollment_id}/approve")
    async def approve_node_pairing(
        request: Request, enrollment_id: str
    ) -> RedirectResponse:
        await _verified_form(request, settings)
        try:
            computer = store.approve_node_enrollment(enrollment_id)
        except (KeyError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(f"/computers/{computer['id']}?connected=1", status_code=303)

    @app.post("/computers/node/pair/{enrollment_id}/reject")
    async def reject_node_pairing(
        request: Request, enrollment_id: str
    ) -> RedirectResponse:
        await _verified_form(request, settings)
        enrollment = store.get_node_enrollment(enrollment_id)
        if enrollment is None:
            raise HTTPException(status_code=404, detail="Node enrollment not found")
        try:
            store.reject_node_enrollment(enrollment_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(
            f"/computers/node/pair?pairing_id={enrollment['pairing_code_id']}",
            status_code=303,
        )

    @app.post("/api/node/enroll", response_class=JSONResponse)
    async def enroll_node(request: Request) -> JSONResponse:
        remote_key = request.client.host if request.client else "unknown"
        if not node_pairing_limiter.allow(remote_key, "enroll"):
            return JSONResponse(
                {"ok": False, "code": "rate_limited", "error": "Too many pairing attempts"},
                status_code=429,
                headers={"Retry-After": "60"},
            )
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise NodeProtocolError("Pairing request is invalid", code="pairing_invalid")
            polling_secret = str(payload.get("polling_secret") or "")
            if not 32 <= len(polling_secret) <= 256:
                raise NodeProtocolError(
                    "Pairing polling secret is invalid", code="pairing_invalid"
                )
            public_key = str(payload.get("public_key") or "")
            fingerprint = public_key_fingerprint(public_key)
            if not secrets.compare_digest(
                fingerprint, str(payload.get("fingerprint") or "")
            ):
                raise NodeProtocolError(
                    "Node fingerprint does not match its key", code="identity_invalid"
                )
            version = validate_protocol_version(payload.get("protocol_version"))
            enrollment = store.submit_node_enrollment(
                code_hash=pairing_code_digest(str(payload.get("code") or "")),
                name=str(payload.get("name") or "Node"),
                public_key=public_key,
                fingerprint=fingerprint,
                protocol_version=version,
                polling_secret_hash=secret_digest(polling_secret),
            )
            if enrollment is None:
                raise NodeProtocolError(
                    "Pairing code is expired, used, or invalid", code="pairing_invalid"
                )
        except (NodeProtocolError, ValueError, json.JSONDecodeError) as exc:
            code = exc.code if isinstance(exc, NodeProtocolError) else "pairing_invalid"
            return JSONResponse(
                {"ok": False, "code": code, "error": str(exc)}, status_code=400
            )
        return JSONResponse(
            {"ok": True, "enrollment_id": enrollment["id"], "status": "pending"},
            status_code=202,
        )

    @app.post("/api/node/enroll/status", response_class=JSONResponse)
    async def node_enrollment_status(request: Request) -> JSONResponse:
        remote_key = request.client.host if request.client else "unknown"
        if not node_pairing_limiter.allow(remote_key, "status"):
            return JSONResponse(
                {"ok": False, "code": "rate_limited", "error": "Too many status requests"},
                status_code=429,
                headers={"Retry-After": "60"},
            )
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("Pairing status request is invalid")
            enrollment = store.get_node_enrollment(
                str(payload.get("enrollment_id") or ""),
                polling_secret_hash=secret_digest(str(payload.get("polling_secret") or "")),
            )
        except (ValueError, json.JSONDecodeError) as exc:
            return JSONResponse(
                {"ok": False, "code": "pairing_invalid", "error": str(exc)},
                status_code=400,
            )
        if enrollment is None:
            return JSONResponse(
                {
                    "ok": False,
                    "code": "pairing_unknown",
                    "error": "Pairing enrollment was not found",
                },
                status_code=404,
            )
        return JSONResponse(
            {
                "ok": True,
                "status": enrollment["status"],
                "node_id": enrollment["computer_id"]
                if enrollment["status"] == "approved"
                else None,
            }
        )

    @app.websocket("/api/node/control")
    async def node_control_socket(websocket: WebSocket, node_id: str = "") -> None:
        await node_core.handle_socket(websocket, node_id)

    @app.get("/computers/new", response_class=HTMLResponse)
    async def new_computer_page(request: Request) -> HTMLResponse:
        locale = locale_from_request(request)
        managed_public_key, managed_key_error = await managed_key_display(locale)
        return templates.TemplateResponse(
            request=request,
            name="computer_new.html",
            context=_context(
                settings,
                title=translate(locale, "title.computer_add"),
                values={},
                error=None,
                managed_public_key=managed_public_key,
                managed_key_error=managed_key_error,
            ),
        )

    @app.post("/computers/probe", response_class=JSONResponse)
    async def probe_computer(request: Request):  # type: ignore[no-untyped-def]
        locale = locale_from_request(request)
        form = await _verified_form(request, settings)
        values = _ssh_form_values(form)
        try:
            target = ssh.resolve_target(values["target"])
            _apply_ssh_overrides(target, values, locale=locale)
            host_key = await asyncio.to_thread(
                ssh.probe_target_host_key, target
            )
        except (OSError, ValueError, SSHBackendError) as exc:
            return JSONResponse(
                {"ok": False, "error": _localized_exception(locale, exc)},
                status_code=400,
            )
        return {
            "ok": True,
            "host": str(target["host"]),
            "port": int(target["port"]),
            "username": str(target["username"]),
            **host_key,
        }

    @app.post("/computers", response_class=HTMLResponse)
    async def create_computer(request: Request):  # type: ignore[no-untyped-def]
        locale = locale_from_request(request)
        form = await _verified_form(request, settings)
        values = _ssh_form_values(form)
        managed_key: dict[str, str] | None = None
        computer: dict[str, Any] | None = None
        try:
            if str(form.get("confirm_fingerprint", "")) != "1":
                raise ValueError(translate(locale, "ssh.error.host_key_required"))
            target = ssh.resolve_target(values["target"])
            _apply_ssh_overrides(target, values, locale=locale)
            probed = await asyncio.to_thread(ssh.probe_target_host_key, target)
            expected_fingerprint = str(form.get("host_fingerprint", ""))
            expected_key_type = str(form.get("host_key_type", ""))
            expected_key_data = str(form.get("host_key_data", ""))
            if (
                probed["host_fingerprint"] != expected_fingerprint
                or probed["host_key_type"] != expected_key_type
                or probed["host_key_data"] != expected_key_data
            ):
                raise ValueError(translate(locale, "ssh.error.host_key_changed"))

            auth_mode = values["auth_mode"]
            identity_file = ""
            temporary = {
                "id": "",
                "host": str(target["host"]),
                "port": int(target["port"]),
                "username": str(target["username"]),
                "ssh_alias": str(target.get("ssh_alias") or ""),
                "identity_file": identity_file,
                "host_key_type": probed["host_key_type"],
                "host_key_data": probed["host_key_data"],
                "host_fingerprint": probed["host_fingerprint"],
                "auth_kind": "key",
            }
            if auth_mode == "password":
                password = str(form.get("password", ""))
                if not password:
                    raise ValueError(translate(locale, "ssh.add.password_required"))
                await asyncio.to_thread(ssh.test_password_connection, temporary, password)
                identity_file = ""
            elif auth_mode == "key":
                managed_key = await asyncio.to_thread(ssh.ensure_managed_key)
                identity_file = managed_key["private_key"]
            elif auth_mode == "existing":
                if values.get("identity_file"):
                    identity_file = str(target.get("identity_file") or "")
            else:
                raise ValueError(translate(locale, "ssh.error.auth_unsupported"))

            temporary["identity_file"] = identity_file
            if auth_mode != "password":
                await asyncio.to_thread(ssh.test_connection, temporary)
            computer = store.create_computer(
                name=values["name"] or values["target"],
                ssh_alias=str(target.get("ssh_alias") or ""),
                host=str(target["host"]),
                port=int(target["port"]),
                username=str(target["username"]),
                identity_file=identity_file,
                auth_kind="password" if auth_mode == "password" else "key",
                host_key_type=probed["host_key_type"],
                host_key_data=probed["host_key_data"],
                host_fingerprint=probed["host_fingerprint"],
            )
            if auth_mode == "password":
                await asyncio.to_thread(ssh.save_password, str(computer["id"]), password)
            ssh.remember_host_key(computer)
        except (OSError, ValueError, SSHBackendError) as exc:
            if computer is not None:
                computer_id = str(computer["id"])
                with contextlib.suppress(OSError, ValueError):
                    ssh.delete_password(computer_id)
                with contextlib.suppress(OSError, ValueError):
                    ssh.forget_host_key(computer_id)
                with contextlib.suppress(RuntimeError):
                    store.delete_computer(computer_id)
            managed_public_key, managed_key_error = await managed_key_display(locale)
            return templates.TemplateResponse(
                request=request,
                name="computer_new.html",
                context=_context(
                    settings,
                    title=translate(locale, "title.computer_add"),
                    values=values,
                    error=_localized_exception(locale, exc),
                    managed_public_key=managed_public_key,
                    managed_key_error=managed_key_error,
                ),
                status_code=400,
            )
        return RedirectResponse(
            f"/open/{computer['id']}?connected=1",
            status_code=303,
        )

    @app.get("/computers/{computer_id}", response_class=HTMLResponse)
    async def computer_page(
        request: Request,
        computer_id: str,
        error: str | None = None,
        connected: bool = False,
        checked: bool = False,
        password_updated: bool = False,
        run_base_updated: bool = False,
        name_updated: bool = False,
    ) -> HTMLResponse:
        locale = locale_from_request(request)
        computer = _require_computer(store, computer_id)
        remote_workspaces = [
            workspaces.require(str(item["id"]))
            for item in store.list_workspaces_for_computer(computer_id)
        ]
        return templates.TemplateResponse(
            request=request,
            name="computer.html",
            context=_context(
                settings,
                title=computer["name"],
                computer=computer,
                remote_workspaces=remote_workspaces,
                error=error or _localized_stored_ssh_error(locale, computer.get("last_error")),
                connected=connected,
                checked=checked,
                password_updated=password_updated,
                run_base_updated=run_base_updated,
                name_updated=name_updated,
                managed_key_path=str(ssh.managed_key_path),
                remote_status=_remote_connection_view(
                    computer, remote=remote, locale=locale
                ),
            ),
        )

    @app.post("/computers/{computer_id}/name")
    async def update_computer_name(
        request: Request, computer_id: str
    ):  # type: ignore[no-untyped-def]
        locale = locale_from_request(request)
        form = await _verified_form(request, settings)
        _require_computer(store, computer_id)
        try:
            store.update_computer_name(computer_id, str(form.get("name", "")))
        except ValueError:
            return RedirectResponse(
                _url_with_query(
                    f"/computers/{computer_id}",
                    error=translate(locale, "ssh.detail.name_invalid"),
                ),
                status_code=303,
            )
        return RedirectResponse(
            _url_with_query(f"/computers/{computer_id}", name_updated=1),
            status_code=303,
        )

    @app.post("/computers/{computer_id}/server-terminal")
    async def open_server_terminal(
        request: Request, computer_id: str
    ):  # type: ignore[no-untyped-def]
        locale = locale_from_request(request)
        await _verified_form(request, settings)
        computer = _require_computer(store, computer_id)
        if computer.get("connection_method") != "ssh":
            raise HTTPException(status_code=404, detail="Server Terminal is not supported")
        try:
            home = await asyncio.to_thread(ssh.home_directory, computer)
            workspace = workspaces.open_server_terminal(computer_id, home)
            await asyncio.to_thread(ssh.ensure_workspace, workspace)
        except (OSError, SSHBackendError) as exc:
            return RedirectResponse(
                _url_with_query(
                    f"/computers/{computer_id}",
                    error=_localized_exception(locale, exc),
                ),
                status_code=303,
            )
        except (RuntimeError, ValueError):
            return RedirectResponse(
                _url_with_query(
                    f"/computers/{computer_id}",
                    error=translate(locale, "server_terminal.open_failed"),
                ),
                status_code=303,
            )
        return RedirectResponse(f"/w/{workspace['id']}/terminal", status_code=303)

    @app.post("/computers/{computer_id}/test")
    async def test_computer(request: Request, computer_id: str):  # type: ignore[no-untyped-def]
        locale = locale_from_request(request)
        form = await _verified_form(request, settings)
        computer = _require_computer(store, computer_id)
        if computer.get("connection_method") != "ssh":
            raise HTTPException(status_code=404, detail="SSH connection test is not supported")
        return_to = str(form.get("return_to") or "")
        try:
            await asyncio.to_thread(ssh.test_connection, computer)
        except SSHBackendError as exc:
            destination = (
                return_to
                if return_to == f"/open/{computer_id}"
                else f"/computers/{computer_id}"
            )
            return RedirectResponse(
                _url_with_query(
                    destination,
                    error=_localized_exception(locale, exc),
                ),
                status_code=303,
            )
        if return_to == f"/open/{computer_id}":
            return RedirectResponse(
                _url_with_query(return_to, connected=1), status_code=303
            )
        return RedirectResponse(
            _url_with_query(f"/computers/{computer_id}", checked=1),
            status_code=303,
        )

    @app.post("/computers/{computer_id}/password")
    async def update_computer_password(
        request: Request, computer_id: str
    ):  # type: ignore[no-untyped-def]
        locale = locale_from_request(request)
        form = await _verified_form(request, settings)
        computer = _require_computer(store, computer_id)
        if str(computer.get("auth_kind") or "key") != "password":
            return RedirectResponse(
                _url_with_query(
                    f"/computers/{computer_id}",
                    error=translate(locale, "ssh.detail.password_not_used"),
                ),
                status_code=303,
            )
        password = str(form.get("password", ""))
        if not password:
            return RedirectResponse(
                _url_with_query(
                    f"/computers/{computer_id}",
                    error=translate(locale, "ssh.add.password_required"),
                ),
                status_code=303,
            )
        try:
            await asyncio.to_thread(ssh.test_password_connection, computer, password)
            await asyncio.to_thread(ssh.save_password, computer_id, password)
        except (OSError, ValueError, SSHBackendError) as exc:
            return RedirectResponse(
                _url_with_query(
                    f"/computers/{computer_id}",
                    error=_localized_exception(locale, exc),
                ),
                status_code=303,
            )
        return RedirectResponse(
            _url_with_query(f"/computers/{computer_id}", password_updated=1),
            status_code=303,
        )

    @app.post("/computers/{computer_id}/run-base")
    async def update_computer_run_base(
        request: Request, computer_id: str
    ):  # type: ignore[no-untyped-def]
        locale = locale_from_request(request)
        form = await _verified_form(request, settings)
        computer = _require_computer(store, computer_id)
        requested = str(form.get("run_base_dir", "")).strip()
        if requested and not requested.startswith("/"):
            return RedirectResponse(
                _url_with_query(
                    f"/computers/{computer_id}",
                    error=translate(locale, "ssh.detail.run_base_error"),
                ),
                status_code=303,
            )
        try:
            preflight = await asyncio.to_thread(
                ssh.preflight_remote_run_target,
                computer,
                run_base_dir=requested or None,
            )
            await asyncio.to_thread(
                store.update_computer_run_base,
                computer_id,
                str(preflight["run_base"]) if requested else None,
            )
        except (OSError, ValueError, SSHBackendError) as exc:
            return RedirectResponse(
                _url_with_query(
                    f"/computers/{computer_id}",
                    error=_localized_exception(locale, exc),
                ),
                status_code=303,
            )
        return RedirectResponse(
            _url_with_query(f"/computers/{computer_id}", run_base_updated=1),
            status_code=303,
        )

    @app.post("/computers/{computer_id}/delete")
    async def delete_computer(request: Request, computer_id: str):  # type: ignore[no-untyped-def]
        locale = locale_from_request(request)
        await _verified_form(request, settings)
        computer = _require_computer(store, computer_id)
        if store.list_remote_runs_for_computer(computer_id):
            return RedirectResponse(
                _url_with_query(
                    f"/computers/{computer_id}",
                    error=translate(locale, "ssh.detail.remove_runs_first"),
                ),
                status_code=303,
            )
        workspace_ids = [
            str(item["id"])
            for item in store.list_registered_workspaces_for_computer(computer_id)
        ]
        terminal_ids = [
            str(terminal["id"])
            for workspace_id in workspace_ids
            for terminal in store.list_terminals(workspace_id)
        ]
        try:
            if computer.get("connection_method") == "node":
                await node_core.revoke(computer_id)
            else:
                await asyncio.to_thread(ssh.delete_password, computer_id)
                await asyncio.to_thread(ssh.forget_host_key, computer_id)
            store.remove_computer_registration(computer_id)
        except (OSError, RuntimeError, ValueError, SSHBackendError) as exc:
            return RedirectResponse(
                _url_with_query(
                    f"/computers/{computer_id}",
                    error=_localized_exception(locale, exc),
                ),
                status_code=303,
            )
        for terminal_id in terminal_ids:
            for socket in active_terminal_websockets.pop(terminal_id, []):
                with contextlib.suppress(RuntimeError):
                    await socket.close(code=4404, reason="Terminal registration removed")
        for workspace_id in workspace_ids:
            recent_file_snapshots.pop(workspace_id, None)
        return RedirectResponse("/open?computer_removed=1", status_code=303)

    @app.post("/computers/{computer_id}/revoke")
    async def revoke_node(request: Request, computer_id: str) -> RedirectResponse:
        await _verified_form(request, settings)
        computer = _require_computer(store, computer_id)
        if computer.get("connection_method") != "node":
            raise HTTPException(status_code=404, detail="Node not found")
        try:
            store.revoke_node(computer_id)
        except KeyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await node_core.revoke(computer_id)
        return RedirectResponse(f"/computers/{computer_id}", status_code=303)

    @app.post("/computers/{computer_id}/workspaces")
    async def create_remote_workspace(request: Request, computer_id: str):  # type: ignore[no-untyped-def]
        locale = locale_from_request(request)
        form = await _verified_form(request, settings)
        computer = _require_computer(store, computer_id)
        remote_path = str(form.get("path", "")).strip()
        display_name = str(form.get("display_name", "")).strip() or None
        created_workspace_id: str | None = None
        try:
            canonical = await remote.validate_workspace_path(computer, remote_path)
            existing = store.find_remote_workspace(computer_id, canonical)
            workspace = workspaces.open_remote(computer_id, canonical, display_name)
            if existing is None:
                created_workspace_id = str(workspace["id"])
            await remote.ensure_workspace(workspace)
        except (ValueError, SSHBackendError, RemoteAccessError) as exc:
            if created_workspace_id:
                store.delete_workspace(created_workspace_id)
            return RedirectResponse(
                f"/open/{computer_id}?error="
                f"{quote(_localized_exception(locale, exc))}",
                status_code=303,
            )
        return RedirectResponse(f"/w/{workspace['id']}/terminal", status_code=303)

    @app.post("/api/workspaces")
    async def open_workspace(request: Request):  # type: ignore[no-untyped-def]
        require_local_workspaces()
        form = await _verified_form(request, settings)
        root_id = str(form.get("root_id", "")).strip()
        if not root_id:
            raise HTTPException(status_code=400, detail="Local folder location is required")
        root_record = store.get_root(root_id)
        if not root_record or str(root_record["path"]).startswith(("ssh://", "node://")):
            raise HTTPException(status_code=404, detail="Local folder location not found")
        workspace = workspaces.open_local(
            str(root_record["path"]), str(form.get("path", "."))
        )
        terminals.ensure_workspace(workspace)
        return RedirectResponse(f"/w/{workspace['id']}/terminal", status_code=303)

    @app.get("/w/{workspace_id}")
    async def workspace_root(workspace_id: str):  # type: ignore[no-untyped-def]
        workspace = _require_workspace(workspaces, workspace_id)
        tab = workspace.get("last_tab") or "terminal"
        if tab not in {"terminal", "files", "recent"}:
            tab = "terminal"
        return RedirectResponse(f"/w/{workspace_id}/{tab}", status_code=303)

    @app.post("/w/{workspace_id}/name")
    async def update_workspace_display_name(
        request: Request, workspace_id: str
    ) -> RedirectResponse:
        locale = locale_from_request(request)
        form = await _verified_form(request, settings)
        workspace = _require_workspace(workspaces, workspace_id)
        try:
            workspaces.update_display_name(
                workspace, str(form.get("display_name", ""))
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=translate(locale, "workspace.name.invalid"),
            ) from exc
        return RedirectResponse(f"/w/{workspace_id}", status_code=303)

    @app.post("/w/{workspace_id}/remove")
    async def remove_workspace_registration(
        request: Request, workspace_id: str
    ) -> RedirectResponse:
        locale = locale_from_request(request)
        form = await _verified_form(request, settings)
        workspace = store.get_workspace(workspace_id)
        if (
            workspace is None
            or workspace.get("workspace_kind") != "workspace"
            or store.get_remote_run_for_workspace(workspace_id) is not None
        ):
            raise HTTPException(status_code=404, detail="Workspace not found")

        terminal_ids = [
            str(terminal["id"]) for terminal in store.list_terminals(workspace_id)
        ]
        return_root_id = str(form.get("return_root_id") or "")
        return_to_local = return_root_id == str(workspace["root_id"])
        try:
            store.remove_workspace_registration(workspace_id)
        except RuntimeError:
            destination = (
                _url_with_query(
                    "/open/local",
                    root=return_root_id,
                    error=translate(locale, "workspace.remove.remote_runs_first"),
                )
                if return_to_local
                else _url_with_query(
                    f"/w/{workspace_id}/{workspace.get('last_tab') or 'terminal'}",
                    error=translate(locale, "workspace.remove.remote_runs_first"),
                )
            )
            return RedirectResponse(destination, status_code=303)
        for terminal_id in terminal_ids:
            for socket in active_terminal_websockets.pop(terminal_id, []):
                with contextlib.suppress(RuntimeError):
                    await socket.close(code=4404, reason="Workspace registration removed")
        file_runs.cleanup_workspace_metadata(workspace_id)
        recent_file_snapshots.pop(workspace_id, None)
        recent_file_cache.pop(workspace_id, None)
        destination = (
            _url_with_query(
                "/open/local", root=return_root_id, workspace_removed=1
            )
            if return_to_local
            else "/?workspace_removed=1"
        )
        return RedirectResponse(destination, status_code=303)

    @app.post("/w/{workspace_id}/run-commands")
    async def replace_workspace_run_commands(
        request: Request, workspace_id: str
    ) -> RedirectResponse:
        locale = locale_from_request(request)
        form = await _verified_form(request, settings)
        workspace = _require_workspace(workspaces, workspace_id)
        if workspace.get("transient") or workspace.get("workspace_kind") != "workspace":
            raise HTTPException(
                status_code=404,
                detail="Workspace commands are available only for persistent Workspaces",
            )
        try:
            store.replace_workspace_commands(
                workspace_id, form.getlist("commands")
            )
        except ValueError:
            return RedirectResponse(
                _url_with_query(
                    f"/w/{workspace_id}/terminal",
                    error=translate(locale, "workspace.run.error.invalid"),
                ),
                status_code=303,
            )
        return RedirectResponse(f"/w/{workspace_id}", status_code=303)

    @app.post("/w/{workspace_id}/run-commands/{slot}")
    async def run_workspace_command(
        request: Request, workspace_id: str, slot: int
    ) -> RedirectResponse:
        locale = locale_from_request(request)
        form = await _verified_form(request, settings)
        workspace = _require_workspace(workspaces, workspace_id)
        if workspace.get("transient") or workspace.get("workspace_kind") != "workspace":
            raise HTTPException(
                status_code=404,
                detail="Workspace commands are available only for persistent Workspaces",
            )
        commands = store.list_workspace_commands(workspace_id)
        if slot < 0 or slot >= len(commands):
            raise HTTPException(status_code=404, detail="Workspace command not found")
        if not secure_compare(
            str(form.get("command_digest") or ""),
            workspace_command_digest(commands[slot]),
        ):
            return RedirectResponse(
                _url_with_query(
                    f"/w/{workspace_id}/terminal",
                    error=translate(locale, "workspace.run.error.changed"),
                ),
                status_code=303,
            )
        launch_id = str(form.get("launch_id") or "")
        try:
            if is_remote(workspace):
                terminal = await remote.run_workspace_command(
                    workspace,
                    slot=slot,
                    command=commands[slot],
                    launch_id=launch_id,
                )
            else:
                terminal = await asyncio.to_thread(
                    terminals.run_workspace_command,
                    workspace,
                    slot=slot,
                    command=commands[slot],
                    launch_id=launch_id,
                )
        except (ValueError, TerminalError, SSHBackendError, RemoteAccessError) as exc:
            return RedirectResponse(
                _url_with_query(
                    f"/w/{workspace_id}/terminal",
                    error=_localized_exception(locale, exc),
                ),
                status_code=303,
            )
        return RedirectResponse(
            _url_with_query(
                f"/w/{workspace_id}/terminal", terminal=str(terminal["id"])
            ),
            status_code=303,
        )

    @app.get("/api/workspaces/{workspace_id}/usage", response_class=JSONResponse)
    async def workspace_usage_status(workspace_id: str) -> dict[str, Any]:
        workspace = _require_workspace(workspaces, workspace_id)
        if workspace.get("transient"):
            raise HTTPException(
                status_code=404,
                detail="Workspace activity is not available for transient Workspaces",
            )

        if remote.is_node(workspace) and not remote.status(workspace["computer"])["online"]:
            view = await workspace_usage.record_failure(
                workspace_id, WorkspaceUsageOffline()
            )
            return view.payload()

        async def collect():  # type: ignore[no-untyped-def]
            if is_remote(workspace):
                return await remote.workspace_usage(workspace)
            return await asyncio.to_thread(terminals.workspace_usage, workspace)

        view = await workspace_usage.observe(workspace_id, collect)
        return view.payload()

    @app.get("/w/{workspace_id}/terminal", response_class=HTMLResponse)
    async def terminal_page(
        request: Request,
        workspace_id: str,
        terminal: str | None = None,
        error: str | None = None,
    ) -> HTMLResponse:
        locale = locale_from_request(request)
        workspace = _require_workspace(workspaces, workspace_id)
        terminal_error = error
        try:
            terminal_list = await ensure_terminal_list(workspace)
        except (SSHBackendError, RemoteAccessError) as exc:
            if not is_remote(workspace):
                raise
            terminal_list = store.list_terminals(workspace_id)
            if not terminal_list:
                raise
            terminal_error = _localized_exception(locale, exc)
        selected = next((item for item in terminal_list if item["id"] == terminal), None)
        selected = selected or terminal_list[0]
        selected_file_run = None
        if selected.get("role") == "file_run" and selected.get("managed_run_id"):
            with contextlib.suppress(KeyError, ValueError):
                run = file_runs.get(str(selected["managed_run_id"]))
                if str(run["workspace_id"]) == workspace_id:
                    if run["state"] in {"preparing", "running"}:
                        with contextlib.suppress(
                            OSError, SSHBackendError, TerminalError
                        ):
                            run = await asyncio.to_thread(
                                file_runs.reconcile, str(run["id"])
                            )
                    selected_file_run = _file_run_status_payload(
                        store, run, locale=locale
                    )
        store.touch_workspace(workspace_id, tab="terminal")
        return templates.TemplateResponse(
            request=request,
            name="terminal.html",
            context=_workspace_context(
                settings,
                workspace,
                active_tab="terminal",
                workspace_commands_supported=remote.supports_capability(
                    workspace, "workspace_command"
                ),
                recent_supported=remote.supports_capability(workspace, "recent"),
                terminals=terminal_list,
                terminal=selected,
                commands=store.list_commands(workspace_id),
                file_run=selected_file_run,
                error=terminal_error,
                current_device_id=str(getattr(request.state, "session", {}).get("id", "")),
                **_workspace_status(store, terminals, workspace),
            ),
        )

    @app.get("/api/terminals/{terminal_id}/presence", response_class=JSONResponse)
    async def terminal_presence(terminal_id: str) -> dict[str, int | str]:
        terminal = store.get_terminal(terminal_id)
        if not terminal:
            raise HTTPException(status_code=404, detail="Terminal not found")
        _require_workspace(workspaces, str(terminal["workspace_id"]))
        return terminal_control.presence(terminal_id)

    @app.post("/w/{workspace_id}/terminals")
    async def create_terminal(request: Request, workspace_id: str):  # type: ignore[no-untyped-def]
        form = await _verified_form(request, settings)
        workspace = _require_workspace(workspaces, workspace_id)
        name = str(form.get("name", "shell"))
        if is_remote(workspace):
            terminal = await remote.create_terminal(workspace, name)
        else:
            terminal = terminals.create_terminal(workspace, name)
        return RedirectResponse(
            f"/w/{workspace_id}/terminal?terminal={terminal['id']}", status_code=303
        )

    @app.post("/w/{workspace_id}/commands/clear")
    async def clear_command_history(request: Request, workspace_id: str):  # type: ignore[no-untyped-def]
        form = await _verified_form(request, settings)
        _require_workspace(workspaces, workspace_id)
        terminal_id = str(form.get("terminal", ""))
        store.clear_commands(workspace_id)
        destination = f"/w/{workspace_id}/terminal"
        if terminal_id:
            terminal = store.get_terminal(terminal_id)
            if terminal and terminal["workspace_id"] == workspace_id:
                destination = _url_with_query(destination, terminal=terminal_id)
        return RedirectResponse(destination, status_code=303)

    @app.post("/w/{workspace_id}/terminals/{terminal_id}")
    async def manage_terminal(
        request: Request, workspace_id: str, terminal_id: str
    ):  # type: ignore[no-untyped-def]
        locale = locale_from_request(request)
        workspace = _require_workspace(workspaces, workspace_id)
        terminal = _require_terminal(store, workspace_id, terminal_id)
        form = await _verified_form(request, settings)
        action = str(form.get("action", "rename"))
        try:
            if str(terminal.get("role") or "shell") != "shell":
                raise ValueError(translate(locale, "terminal.managed_locked"))
            if action == "rename":
                name = str(form.get("name", "shell"))
                if is_remote(workspace):
                    updated = await remote.rename_terminal(workspace, terminal, name)
                else:
                    updated = terminals.rename_terminal(workspace, terminal, name)
                return RedirectResponse(
                    f"/w/{workspace_id}/terminal?terminal={updated['id']}", status_code=303
                )
            if action == "delete":
                if is_remote(workspace):
                    remaining = await remote.close_terminal(workspace, terminal)
                else:
                    remaining = terminals.close_terminal(workspace, terminal)
                selected = remaining[0]
                return RedirectResponse(
                    f"/w/{workspace_id}/terminal?terminal={selected['id']}", status_code=303
                )
            raise ValueError("Unknown terminal action")
        except (ValueError, TerminalError, SSHBackendError, RemoteAccessError) as exc:
            return RedirectResponse(
                f"/w/{workspace_id}/terminal?terminal={terminal_id}&error="
                f"{quote(_localized_exception(locale, exc))}",
                status_code=303,
            )

    @app.get(
        "/w/{workspace_id}/terminal/{terminal_id}/scrollback", response_class=HTMLResponse
    )
    async def terminal_scrollback(
        request: Request,
        workspace_id: str,
        terminal_id: str,
        recent: int = 2000,
        history_only: bool = False,
    ) -> Response:
        workspace = _require_workspace(workspaces, workspace_id)
        terminal = _require_terminal(store, workspace_id, terminal_id)
        if is_remote(workspace):
            output = await remote.capture_scrollback(
                workspace,
                terminal,
                recent,
                history_only=history_only,
            )
        else:
            output = terminals.capture_scrollback(
                workspace,
                terminal,
                recent,
                history_only=history_only,
            )
        if "text/plain" in request.headers.get("accept", ""):
            response_headers: dict[str, str] = {}
            if history_only:
                etag = f'"{hashlib.sha256(output.encode()).hexdigest()}"'
                response_headers = {
                    "Cache-Control": "private, no-cache",
                    "ETag": etag,
                    "Vary": "Accept",
                }
                if_none_match = request.headers.get("if-none-match", "")
                if any(
                    candidate.strip().removeprefix("W/") in {"*", etag}
                    for candidate in if_none_match.split(",")
                ):
                    return Response(status_code=304, headers=response_headers)
            return PlainTextResponse(output, headers=response_headers)
        return templates.TemplateResponse(
            request=request,
            name="scrollback.html",
            context=_workspace_context(
                settings,
                workspace,
                active_tab="terminal",
                workspace_commands_supported=remote.supports_capability(
                    workspace, "workspace_command"
                ),
                recent_supported=remote.supports_capability(workspace, "recent"),
                terminal=terminal,
                output=output,
                **_workspace_status(store, terminals, workspace),
            ),
        )

    @app.get("/w/{workspace_id}/files", response_class=HTMLResponse)
    async def file_browser(
        request: Request,
        workspace_id: str,
        path: str = ".",
        error: str | None = None,
        uploaded: int = 0,
        noise: bool = False,
        page: int = 1,
        q: str = "",
    ) -> HTMLResponse:
        locale = locale_from_request(request)
        workspace = _require_workspace(workspaces, workspace_id)
        directory, all_entries = await list_workspace_dir(workspace, path)
        relative = (
            _normalize_relative_path(path)
            if is_remote(workspace)
            else _workspace_relative(workspace["path"], directory)
        )
        visible_entries = [
            entry
            for entry in all_entries
            if not _is_internal_state_entry(settings, workspace, relative, entry.name)
        ]
        noise_count = sum(_file_browser_entry_is_noise(entry) for entry in visible_entries)
        filtered_entries = (
            visible_entries
            if noise
            else [
                entry
                for entry in visible_entries
                if not _file_browser_entry_is_noise(entry)
            ]
        )
        query = q.strip()[:120]
        if query:
            needle = query.casefold()
            filtered_entries = [
                entry for entry in filtered_entries if needle in entry.name.casefold()
            ]
        total_entries = len(filtered_entries)
        page_count = max(
            1,
            (total_entries + FILE_BROWSER_PAGE_SIZE - 1) // FILE_BROWSER_PAGE_SIZE,
        )
        current_page = max(1, min(page, page_count))
        page_start = (current_page - 1) * FILE_BROWSER_PAGE_SIZE
        entries = filtered_entries[page_start : page_start + FILE_BROWSER_PAGE_SIZE]
        store.touch_workspace(workspace_id, tab="files")
        context = _workspace_context(
            settings,
            workspace,
            active_tab="files",
            workspace_commands_supported=remote.supports_capability(
                workspace, "workspace_command"
            ),
            recent_supported=remote.supports_capability(workspace, "recent"),
            path=relative,
            entries=entries,
            breadcrumbs=_breadcrumbs(relative),
            error=error,
            uploaded=uploaded,
            show_noise=noise,
            noise_count=noise_count,
            current_page=current_page,
            page_count=page_count,
            total_entries=total_entries,
            query=query,
            terminal_editor_supported=remote.supports_capability(
                workspace, "terminal_editor"
            ),
            can_remote_run_source=(
                not workspace.get("transient")
                and (
                    not remote.is_node(workspace)
                    or remote.supports_capability(workspace, "remote_run_source")
                )
            ),
            max_upload_bytes=settings.max_upload_bytes,
            format_size=_format_size,
            format_time_ns=lambda value: _relative_time_ns(value, locale),
            **_workspace_status(store, terminals, workspace),
        )
        template_name = (
            "_file_results.html"
            if request.headers.get("x-termroom-partial") == "file-results"
            else "files.html"
        )
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context=context,
        )

    @app.post("/w/{workspace_id}/terminal-editor")
    async def open_file_in_terminal_editor(
        request: Request, workspace_id: str
    ) -> RedirectResponse:
        locale = locale_from_request(request)
        workspace = _require_workspace(workspaces, workspace_id)
        form = await _verified_form(request, settings)
        relative_path = str(form.get("path") or "")
        parent = str(form.get("parent") or ".")
        try:
            entry = await stat_workspace_file(workspace, relative_path)
            if entry.is_dir:
                raise ValueError("Folders cannot be opened in Vim")
            terminal = await open_workspace_terminal_editor(
                workspace, entry.relative_path
            )
        except (
            OSError,
            RemoteAccessError,
            SSHBackendError,
            TerminalError,
            UnsupportedFileError,
            ValueError,
        ) as exc:
            return RedirectResponse(
                _url_with_query(
                    f"/w/{workspace_id}/files",
                    path=parent,
                    error=_localized_exception(locale, exc),
                ),
                status_code=303,
            )
        return RedirectResponse(
            _url_with_query(
                f"/w/{workspace_id}/terminal", terminal=str(terminal["id"])
            ),
            status_code=303,
        )

    @app.get("/w/{workspace_id}/view/{file_path:path}", response_class=HTMLResponse)
    async def file_view(
        request: Request,
        workspace_id: str,
        file_path: str,
        mode: str = "head",
        offset: int = 0,
    ) -> HTMLResponse:
        locale = locale_from_request(request)
        workspace = _require_workspace(workspaces, workspace_id)
        entry = await stat_workspace_file(workspace, file_path)
        if entry.is_dir:
            return RedirectResponse(
                f"/w/{workspace_id}/files?path={quote(file_path, safe='')}",
                status_code=303,
            )

        content_type = workspace_content_type(workspace, file_path)
        kind = _file_view_kind(file_path, content_type)
        preview_too_large = _inline_media_too_large(content_type, entry.size)
        if preview_too_large and kind in {"image", "pdf"}:
            kind = "binary"
        preview = None
        json_content = None
        csv_header: list[str] = []
        csv_rows: list[list[str]] = []
        preview_error = None

        if kind in {"text", "json", "csv"}:
            try:
                preview = await read_workspace_preview(workspace, file_path, mode, offset)
                if kind == "json" and not preview.truncated:
                    try:
                        parsed = json.loads(preview.content)
                        json_content = json.dumps(parsed, ensure_ascii=False, indent=2)
                    except json.JSONDecodeError:
                        kind = "text"
                elif kind == "csv" and not preview.truncated:
                    reader = csv.reader(io.StringIO(preview.content))
                    rows = list(reader)[:101]
                    if rows:
                        csv_header = rows[0]
                        csv_rows = rows[1:]
            except (UnsupportedFileError, SSHBackendError, OSError, ValueError) as exc:
                preview_error = _localized_exception(locale, exc)
                kind = "binary"

        copy_text = None
        if json_content is not None:
            copy_text = json_content
        elif preview is not None and kind in {"text", "json", "csv"}:
            copy_text = preview.content
        sensitive_file = _is_sensitive_file_name(entry.name)

        preview_previous_offset = 0
        preview_next_offset = 0
        if preview is not None:
            preview_previous_offset = max(0, preview.offset - settings.max_preview_bytes)
            preview_next_offset = min(preview.size, preview.offset + preview.bytes_read)

        store.touch_workspace(workspace_id, tab="files")
        return templates.TemplateResponse(
            request=request,
            name="file_view.html",
            context=_workspace_context(
                settings,
                workspace,
                active_tab="files",
                workspace_commands_supported=remote.supports_capability(
                    workspace, "workspace_command"
                ),
                recent_supported=remote.supports_capability(workspace, "recent"),
                entry=entry,
                file_path=file_path,
                content_type=content_type,
                view_kind=kind,
                preview=preview,
                json_content=json_content,
                csv_header=csv_header,
                csv_rows=csv_rows,
                preview_error=preview_error,
                preview_too_large=preview_too_large,
                copy_text=copy_text,
                sensitive_file=sensitive_file,
                preview_previous_offset=preview_previous_offset,
                preview_next_offset=preview_next_offset,
                can_edit=(
                    kind in {"text", "json", "csv"}
                    and entry.size <= settings.max_edit_bytes
                ),
                terminal_editor_supported=remote.supports_capability(
                    workspace, "terminal_editor"
                ),
                format_size=_format_size,
                format_time_ns=lambda value: _relative_time_ns(value, locale),
                **_workspace_status(store, terminals, workspace),
            ),
        )

    @app.get("/w/{workspace_id}/raw/{file_path:path}")
    async def raw_file(
        request: Request, workspace_id: str, file_path: str
    ):  # type: ignore[no-untyped-def]
        workspace = _require_workspace(workspaces, workspace_id)
        entry = await stat_workspace_file(workspace, file_path)
        if entry.is_dir:
            raise HTTPException(status_code=415, detail="Directories cannot be previewed inline")
        content_type = workspace_content_type(workspace, file_path)
        if content_type not in {
            "image/png",
            "image/jpeg",
            "image/gif",
            "image/webp",
            "image/avif",
            "application/pdf",
        }:
            raise HTTPException(status_code=415, detail="Inline preview is not allowed")
        if _inline_media_too_large(content_type, entry.size):
            raise HTTPException(status_code=413, detail="Inline preview is too large")
        if is_remote(workspace):
            range_request = _parse_single_byte_range(request.headers.get("range"), entry.size)
            if request.headers.get("range") and range_request is None:
                return Response(
                    status_code=416,
                    headers={
                        "Content-Range": f"bytes */{entry.size}",
                        "Accept-Ranges": "bytes",
                    },
                )
            start, end = range_request or (0, max(0, entry.size - 1))
            length = 0 if entry.size == 0 else end - start + 1
            headers = {
                "Content-Disposition": _content_disposition(entry.name, "inline"),
                "X-Content-Type-Options": "nosniff",
                "Content-Length": str(length),
                "Accept-Ranges": "bytes",
            }
            if range_request is not None:
                headers["Content-Range"] = f"bytes {start}-{end}/{entry.size}"
            return StreamingResponse(
                remote.download_stream(
                    workspace, file_path, offset=start, length=length
                ),
                media_type=content_type,
                headers=headers,
                status_code=206 if range_request is not None else 200,
            )
        target = files.resolve_regular_file(workspace["path"], file_path)
        response = FileResponse(
            target,
            media_type=content_type,
            filename=target.name,
            content_disposition_type="inline",
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/w/{workspace_id}/download/{file_path:path}")
    async def download_file(
        request: Request, workspace_id: str, file_path: str
    ):  # type: ignore[no-untyped-def]
        workspace = _require_workspace(workspaces, workspace_id)
        entry = await stat_workspace_file(workspace, file_path)
        if entry.is_dir:
            raise HTTPException(status_code=415, detail="Folder download is not supported yet")
        if is_remote(workspace):
            range_request = _parse_single_byte_range(request.headers.get("range"), entry.size)
            if request.headers.get("range") and range_request is None:
                return Response(
                    status_code=416,
                    headers={
                        "Content-Range": f"bytes */{entry.size}",
                        "Accept-Ranges": "bytes",
                    },
                )
            start, end = range_request or (0, max(0, entry.size - 1))
            length = 0 if entry.size == 0 else end - start + 1
            headers = {
                "Content-Disposition": _content_disposition(entry.name, "attachment"),
                "X-Content-Type-Options": "nosniff",
                "Content-Length": str(length),
                "Accept-Ranges": "bytes",
            }
            if range_request is not None:
                headers["Content-Range"] = f"bytes {start}-{end}/{entry.size}"
            return StreamingResponse(
                remote.download_stream(
                    workspace, file_path, offset=start, length=length
                ),
                media_type="application/octet-stream",
                headers=headers,
                status_code=206 if range_request is not None else 200,
            )
        target = files.resolve_regular_file(workspace["path"], file_path)
        response = FileResponse(
            target,
            media_type="application/octet-stream",
            filename=target.name,
            content_disposition_type="attachment",
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/w/{workspace_id}/archive/{file_path:path}")
    async def download_folder_archive(workspace_id: str, file_path: str):  # type: ignore[no-untyped-def]
        workspace = _require_workspace(workspaces, workspace_id)
        normalized = _normalize_relative_path(file_path)
        entry = await stat_workspace_file(workspace, normalized)
        if not entry.is_dir:
            return RedirectResponse(
                f"/w/{workspace_id}/download/{quote(normalized, safe='/')}", status_code=303
            )
        parent = _relative_parent(normalized)
        archive_path = await build_workspace_archive(workspace, parent, [normalized])
        safe_name = "".join(
            character if character.isalnum() or character in {"-", "_", "."} else "-"
            for character in entry.name
        ).strip("-.")
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=f"{safe_name or 'folder'}.zip",
            content_disposition_type="attachment",
            background=BackgroundTask(os.unlink, archive_path),
        )

    @app.post("/w/{workspace_id}/files/archive")
    async def download_file_archive(
        request: Request, workspace_id: str
    ):  # type: ignore[no-untyped-def]
        locale = locale_from_request(request)
        workspace = _require_workspace(workspaces, workspace_id)
        form = await _verified_form(request, settings)
        parent = str(form.get("parent", "."))
        selected_paths = [str(value) for value in form.getlist("paths")]
        if not selected_paths:
            return RedirectResponse(
                _url_with_query(
                    f"/w/{workspace_id}/files",
                    path=parent,
                    error=translate(locale, "files.error.no_selection"),
                ),
                status_code=303,
            )
        if len(selected_paths) > FILE_BROWSER_PAGE_SIZE:
            return RedirectResponse(
                _url_with_query(
                    f"/w/{workspace_id}/files",
                    path=parent,
                    error=translate(locale, "files.error.too_many_selection"),
                ),
                status_code=303,
            )
        try:
            archive_path = await build_workspace_archive(
                workspace, parent, selected_paths
            )
        except (
            OSError,
            ValueError,
            PathBoundaryError,
            UnsupportedFileError,
            SSHBackendError,
        ) as exc:
            return RedirectResponse(
                _url_with_query(
                    f"/w/{workspace_id}/files",
                    path=parent,
                    error=_localized_exception(locale, exc),
                ),
                status_code=303,
            )
        safe_name = "".join(
            character
            if character.isalnum() or character in {"-", "_", "."}
            else "-"
            for character in str(workspace["display_name"])
        ).strip("-.")
        filename = f"{safe_name or 'workspace'}-files.zip"
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=filename,
            content_disposition_type="attachment",
            background=BackgroundTask(os.unlink, archive_path),
        )

    @app.post("/w/{workspace_id}/files/upload")
    async def upload_files(request: Request, workspace_id: str):  # type: ignore[no-untyped-def]
        locale = locale_from_request(request)
        workspace = _require_workspace(workspaces, workspace_id)
        form = await _verified_form(request, settings)
        parent = str(form.get("parent", "."))
        overwrite = str(form.get("overwrite", "0")) == "1"
        uploads = [item for item in form.getlist("files") if isinstance(item, UploadFile)]
        if not uploads:
            return RedirectResponse(
                _url_with_query(
                    f"/w/{workspace_id}/files",
                    path=parent,
                    error=translate(locale, "files.error.no_upload"),
                ),
                status_code=303,
            )

        seen_names: set[str] = set()
        uploaded_count = 0
        try:
            for upload in uploads:
                filename = upload.filename or ""
                if filename in seen_names:
                    raise ValueError(
                        translate(locale, "files.error.duplicate_upload", name=filename)
                    )
                seen_names.add(filename)

            if is_remote(workspace):
                _, current_entries = await list_workspace_dir(workspace, parent)
                existing_names = {item.name for item in current_entries}
                if not overwrite:
                    conflicts = [name for name in seen_names if name in existing_names]
                    if conflicts:
                        raise FileExistsError(
                            translate(locale, "files.error.exists", name=conflicts[0])
                        )
                for upload in uploads:
                    await remote.upload(
                        workspace,
                        parent,
                        upload,
                        overwrite=overwrite,
                        max_bytes=settings.max_upload_bytes,
                    )
                    uploaded_count += 1
            else:
                targets: list[tuple[UploadFile, Path]] = []
                ensure_exposed_local_path(workspace, parent)
                for upload in uploads:
                    filename = upload.filename or ""
                    target = files.upload_target(workspace["path"], parent, filename)
                    if target.exists() and not overwrite:
                        raise FileExistsError(
                            translate(locale, "files.error.exists", name=filename)
                        )
                    targets.append((upload, target))

                for upload, target in targets:
                    await _store_upload(
                        upload,
                        target,
                        settings.max_upload_bytes,
                        locale=locale,
                    )
                    uploaded_count += 1
        except (
            ValueError,
            FileExistsError,
            OSError,
            UnsupportedFileError,
            SSHBackendError,
            RemoteAccessError,
        ) as exc:
            return RedirectResponse(
                _url_with_query(
                    f"/w/{workspace_id}/files",
                    path=parent,
                    error=_localized_exception(locale, exc),
                ),
                status_code=303,
            )
        finally:
            for upload in uploads:
                await upload.close()

        return RedirectResponse(
            _url_with_query(
                f"/w/{workspace_id}/files",
                path=parent,
                uploaded=uploaded_count,
            ),
            status_code=303,
        )

    @app.post("/w/{workspace_id}/files/upload-check", response_class=JSONResponse)
    async def upload_check(request: Request, workspace_id: str) -> JSONResponse:
        locale = locale_from_request(request)
        _verified_csrf_header(request, settings)
        workspace = _require_workspace(workspaces, workspace_id)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError(translate(locale, "files.error.invalid_selection"))
            parent = str(payload.get("parent", "."))
            raw_names = payload.get("names", [])
            if not isinstance(raw_names, list) or len(raw_names) > 1000:
                raise ValueError(translate(locale, "files.error.invalid_selection"))
            names = [str(name) for name in raw_names]
            name_counts = Counter(names)
            duplicate = next((name for name in names if name_counts[name] > 1), None)
            if duplicate is not None:
                raise ValueError(
                    translate(locale, "files.error.duplicate_upload", name=duplicate)
                )

            conflicts: list[dict[str, Any]] = []
            if is_remote(workspace):
                _, entries = await list_workspace_dir(workspace, parent)
                existing = {entry.name: entry for entry in entries}
                for name in names:
                    _normalize_upload_filename(name)
                    entry = existing.get(name)
                    if entry is None:
                        continue
                    if entry.is_dir:
                        raise UnsupportedFileError("Upload target is not a regular file")
                    conflicts.append(
                        {
                            "name": name,
                            "size": entry.size,
                            "mtime": _relative_time_ns(entry.mtime_ns, locale),
                        }
                    )
            else:
                ensure_exposed_local_path(workspace, parent)
                for name in names:
                    target = files.upload_target(workspace["path"], parent, name)
                    if not target.exists():
                        continue
                    stat = target.stat()
                    conflicts.append(
                        {
                            "name": name,
                            "size": stat.st_size,
                            "mtime": _relative_time_ns(stat.st_mtime_ns, locale),
                        }
                    )
        except (
            ValueError,
            OSError,
            PathBoundaryError,
            UnsupportedFileError,
            SSHBackendError,
            json.JSONDecodeError,
        ) as exc:
            return JSONResponse(
                {"ok": False, "error": _localized_exception(locale, exc)},
                status_code=_upload_error_status(exc),
            )
        return JSONResponse({"ok": True, "conflicts": conflicts})

    @app.post("/w/{workspace_id}/files/upload-stream", response_class=JSONResponse)
    async def upload_file_stream(
        request: Request,
        workspace_id: str,
        parent: str = ".",
        filename: str = "",
        overwrite: bool = False,
    ) -> JSONResponse:
        locale = locale_from_request(request)
        _verified_csrf_header(request, settings)
        workspace = _require_workspace(workspaces, workspace_id)
        content_length = request.headers.get("content-length")
        try:
            if content_length and int(content_length) > settings.max_upload_bytes:
                raise ValueError("Upload exceeds the configured size limit")
            if is_remote(workspace):
                await remote.upload_stream(
                    workspace,
                    parent,
                    filename,
                    request.stream(),
                    overwrite=overwrite,
                    max_bytes=settings.max_upload_bytes,
                )
            else:
                ensure_exposed_local_path(workspace, parent)
                target = files.upload_target(workspace["path"], parent, filename)
                if target.exists() and not overwrite:
                    raise FileExistsError(filename)
                await _store_request_upload(
                    request,
                    target,
                    settings.max_upload_bytes,
                    overwrite=overwrite,
                )
        except (
            ValueError,
            FileExistsError,
            OSError,
            PathBoundaryError,
            UnsupportedFileError,
            SSHBackendError,
            RemoteAccessError,
        ) as exc:
            status_code = _upload_error_status(exc)
            return JSONResponse(
                {"ok": False, "error": _localized_exception(locale, exc)},
                status_code=status_code,
            )
        return JSONResponse({"ok": True, "name": filename})

    async def editor_file_run_context(
        workspace: dict[str, Any],
        snapshot: FileSnapshot,
        *,
        locale: str,
        selected_run_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        runner = None
        with contextlib.suppress(
            FileNotFoundError,
            FileRunError,
            OSError,
            RemoteAccessError,
            SSHBackendError,
            UnsupportedFileError,
            ValueError,
        ):
            runner = await asyncio.to_thread(
                file_runs.runner_for_file,
                workspace,
                snapshot.relative_path,
            )

        active = file_runs.active_for_workspace(str(workspace["id"]))
        if active is not None:
            with contextlib.suppress(
                KeyError,
                OSError,
                RemoteAccessError,
                SSHBackendError,
                TerminalError,
                ValueError,
            ):
                active = await asyncio.to_thread(
                    file_runs.reconcile, str(active["id"])
                )
            if active["state"] not in {"preparing", "running"}:
                active = None
        latest = file_runs.latest_for_file(
            str(workspace["id"]), snapshot.relative_path
        )
        if selected_run_id:
            with contextlib.suppress(KeyError, ValueError):
                selected = file_runs.get(selected_run_id)
                if (
                    str(selected["workspace_id"]) == str(workspace["id"])
                    and str(selected["relative_path"]) == snapshot.relative_path
                ):
                    latest = selected

        def file_run_view(run: Mapping[str, Any] | None) -> dict[str, Any] | None:
            if run is None:
                return None
            view = _file_run_status_payload(store, run, locale=locale)
            source_digest = str(run.get("source_digest") or "")
            view["source_changed"] = bool(
                source_digest
                and str(run.get("relative_path") or "") == snapshot.relative_path
                and source_digest != snapshot.digest
            )
            return view

        return {
            "file_run_supported": runner is not None,
            "file_run_runner_id": runner.id if runner is not None else None,
            "file_run_idempotency_key": idempotency_key or str(uuid.uuid4()),
            "active_file_run": file_run_view(active),
            "latest_file_run": file_run_view(latest),
        }

    async def render_editor(
        request: Request,
        workspace: dict[str, Any],
        snapshot: FileSnapshot,
        *,
        saved: bool,
        conflict: str | None,
        save_error: str | None,
        run_error: str | None,
        editor_unsaved: bool,
        terminal_editor_error: str | None = None,
        submitted_content: str | None = None,
        selected_run_id: str | None = None,
        idempotency_key: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        locale = locale_from_request(request)
        values: dict[str, Any] = {
            "snapshot": snapshot,
            "newline_style": editor_newline_style(snapshot.content),
            "saved": saved,
            "conflict": conflict,
            "save_error": save_error,
            "run_error": run_error,
            "terminal_editor_error": terminal_editor_error,
            "editor_unsaved": editor_unsaved,
            "terminal_editor_supported": remote.supports_capability(
                workspace, "terminal_editor"
            ),
            **await editor_file_run_context(
                workspace,
                snapshot,
                locale=locale,
                selected_run_id=selected_run_id,
                idempotency_key=idempotency_key,
            ),
            **_workspace_status(store, terminals, workspace),
        }
        if submitted_content is not None:
            values["submitted_content"] = submitted_content
        return templates.TemplateResponse(
            request=request,
            name="editor.html",
            context=_workspace_context(
                settings,
                workspace,
                active_tab="files",
                workspace_commands_supported=remote.supports_capability(
                    workspace, "workspace_command"
                ),
                recent_supported=remote.supports_capability(workspace, "recent"),
                **values,
            ),
            status_code=status_code,
        )

    @app.get("/w/{workspace_id}/edit/{file_path:path}", response_class=HTMLResponse)
    async def editor(request: Request, workspace_id: str, file_path: str) -> HTMLResponse:
        workspace = _require_workspace(workspaces, workspace_id)
        snapshot = await read_workspace_text(workspace, file_path)
        return await render_editor(
            request,
            workspace,
            snapshot,
            saved=request.query_params.get("saved") == "1",
            conflict=None,
            save_error=None,
            run_error=request.query_params.get("file_run_error"),
            editor_unsaved=False,
            selected_run_id=request.query_params.get("run"),
        )

    @app.post("/w/{workspace_id}/edit/{file_path:path}", response_class=HTMLResponse)
    async def save_file(request: Request, workspace_id: str, file_path: str):  # type: ignore[no-untyped-def]
        workspace = _require_workspace(workspaces, workspace_id)
        form = await _verified_form(
            request,
            settings,
            max_part_size=settings.max_edit_bytes * 3 + 1024,
        )
        locale = locale_from_request(request)
        content = normalize_editor_newlines(
            str(form.get("content", "")), str(form.get("newline", "lf"))
        )
        expected_digest = str(form.get("digest", ""))
        expected_mtime_ns = int(str(form.get("mtime_ns", "0")))
        intent = str(form.get("intent", "save"))
        idempotency_key = str(form.get("file_run_idempotency_key", ""))

        if intent == "save_and_run":
            existing = store.get_file_run_by_idempotency(
                workspace_id, idempotency_key
            )
            if existing is not None:
                submitted_digest = file_digest(content.encode("utf-8"))
                if (
                    str(existing["relative_path"]) == file_path
                    and str(existing["source_digest"]) == submitted_digest
                ):
                    return RedirectResponse(
                        _file_run_destination(store, existing, prefer_terminal=True),
                        status_code=303,
                    )
                current = await read_workspace_text(workspace, file_path)
                return await render_editor(
                    request,
                    workspace,
                    current,
                    saved=False,
                    conflict=None,
                    save_error=None,
                    run_error=translate(locale, "file_run.error.idempotency_conflict"),
                    editor_unsaved=True,
                    submitted_content=content,
                    idempotency_key=idempotency_key,
                    status_code=409,
                )
        try:
            saved_snapshot = await write_workspace_text(
                workspace,
                file_path,
                content,
                expected_digest=expected_digest,
                expected_mtime_ns=expected_mtime_ns,
            )
        except FileConflictError as exc:
            current = await read_workspace_text(workspace, file_path)
            return await render_editor(
                request,
                workspace,
                current,
                submitted_content=content,
                saved=False,
                conflict=_localized_exception(locale, exc),
                save_error=None,
                run_error=None,
                editor_unsaved=True,
                idempotency_key=idempotency_key or None,
                status_code=409,
            )
        except (
            OSError,
            RemoteAccessError,
            SSHBackendError,
            UnsupportedFileError,
            ValueError,
        ) as exc:
            try:
                current = await read_workspace_text(workspace, file_path)
            except (
                OSError,
                RemoteAccessError,
                SSHBackendError,
                UnsupportedFileError,
                ValueError,
            ):
                current = FileSnapshot(
                    path=Path(file_path),
                    relative_path=file_path,
                    content="",
                    digest=expected_digest,
                    mtime_ns=expected_mtime_ns,
                )
            return await render_editor(
                request,
                workspace,
                current,
                submitted_content=content,
                saved=False,
                conflict=None,
                save_error=_localized_exception(locale, exc),
                run_error=None,
                editor_unsaved=True,
                idempotency_key=idempotency_key or None,
                status_code=502
                if isinstance(exc, (RemoteAccessError, SSHBackendError))
                else 409,
            )

        if intent == "save_and_run":
            try:
                run = await asyncio.to_thread(
                    file_runs.start,
                    workspace,
                    saved_snapshot.relative_path,
                    expected_digest=saved_snapshot.digest,
                    idempotency_key=idempotency_key,
                )
            except FileRunConflict as exc:
                return await render_editor(
                    request,
                    workspace,
                    saved_snapshot,
                    saved=True,
                    conflict=None,
                    save_error=None,
                    run_error=_localized_file_run_exception(locale, exc),
                    editor_unsaved=False,
                    idempotency_key=idempotency_key,
                    status_code=409,
                )
            except (
                FileConflictError,
                FileRunError,
                OSError,
                RemoteAccessError,
                SSHBackendError,
                UnsupportedFileError,
                ValueError,
            ) as exc:
                return await render_editor(
                    request,
                    workspace,
                    saved_snapshot,
                    saved=True,
                    conflict=None,
                    save_error=None,
                    run_error=_localized_file_run_exception(locale, exc),
                    editor_unsaved=False,
                    idempotency_key=idempotency_key or None,
                    status_code=502
                    if isinstance(exc, (RemoteAccessError, SSHBackendError))
                    else 409,
                )
            file_runs.wake()
            return RedirectResponse(
                _file_run_destination(store, run, prefer_terminal=True),
                status_code=303,
            )
        if intent == "save_and_vim":
            try:
                terminal = await open_workspace_terminal_editor(
                    workspace, saved_snapshot.relative_path
                )
            except (
                OSError,
                RemoteAccessError,
                SSHBackendError,
                TerminalError,
                UnsupportedFileError,
                ValueError,
            ) as exc:
                return await render_editor(
                    request,
                    workspace,
                    saved_snapshot,
                    saved=True,
                    conflict=None,
                    save_error=None,
                    run_error=None,
                    terminal_editor_error=_localized_exception(locale, exc),
                    editor_unsaved=False,
                    idempotency_key=idempotency_key or None,
                    status_code=502
                    if isinstance(exc, (RemoteAccessError, SSHBackendError))
                    else 409,
                )
            return RedirectResponse(
                _url_with_query(
                    f"/w/{workspace_id}/terminal", terminal=str(terminal["id"])
                ),
                status_code=303,
            )
        return RedirectResponse(
            _url_with_query(
                f"/w/{workspace_id}/edit/{quote(file_path, safe='/')}",
                saved=1,
            ),
            status_code=303,
        )

    @app.get("/api/file-runs/{run_id}/status", response_class=JSONResponse)
    async def file_run_status(request: Request, run_id: str) -> JSONResponse:
        locale = locale_from_request(request)
        try:
            run = await asyncio.to_thread(file_runs.get, run_id)
            if run["state"] in {"preparing", "running"}:
                run = await asyncio.to_thread(file_runs.reconcile, run_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="File Run not found") from exc
        except (OSError, RemoteAccessError, SSHBackendError, TerminalError) as exc:
            return JSONResponse(
                {
                    "ok": False,
                    "error": _localized_file_run_exception(locale, exc),
                },
                status_code=502,
            )
        return JSONResponse(
            {"ok": True, **_file_run_status_payload(store, run, locale=locale)}
        )

    @app.post("/file-runs/{run_id}/stop")
    async def stop_file_run(request: Request, run_id: str) -> RedirectResponse:
        locale = locale_from_request(request)
        form = await _verified_form(request, settings)
        try:
            current = file_runs.get(run_id)
            result = await asyncio.to_thread(file_runs.stop, run_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="File Run not found") from exc
        except (OSError, RemoteAccessError, SSHBackendError, TerminalError) as exc:
            return RedirectResponse(
                _file_run_editor_url(
                    current,
                    file_run_error=_localized_file_run_exception(locale, exc),
                ),
                status_code=303,
            )
        file_runs.wake()
        return RedirectResponse(
            _file_run_destination(
                store,
                result["run"],
                prefer_terminal=str(form.get("return_to", "")) == "terminal",
            ),
            status_code=303,
        )

    @app.post("/file-runs/{run_id}/kill")
    async def kill_file_run(request: Request, run_id: str) -> RedirectResponse:
        locale = locale_from_request(request)
        form = await _verified_form(request, settings)
        try:
            current = file_runs.get(run_id)
            run = await asyncio.to_thread(file_runs.kill, run_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="File Run not found") from exc
        except (OSError, RemoteAccessError, SSHBackendError, TerminalError) as exc:
            return RedirectResponse(
                _file_run_editor_url(
                    current,
                    file_run_error=_localized_file_run_exception(locale, exc),
                ),
                status_code=303,
            )
        file_runs.wake()
        return RedirectResponse(
            _file_run_destination(
                store,
                run,
                prefer_terminal=str(form.get("return_to", "")) == "terminal",
            ),
            status_code=303,
        )

    @app.post("/w/{workspace_id}/diff/{file_path:path}", response_class=PlainTextResponse)
    async def diff_file(request: Request, workspace_id: str, file_path: str) -> str:
        locale = locale_from_request(request)
        workspace = _require_workspace(workspaces, workspace_id)
        form = await _verified_form(request, settings)
        snapshot = await read_workspace_text(workspace, file_path)
        return files.unified_diff(snapshot, str(form.get("content", ""))) or translate(
            locale, "editor.no_diff"
        )

    @app.post("/w/{workspace_id}/files/create")
    async def create_file_entry(request: Request, workspace_id: str):  # type: ignore[no-untyped-def]
        locale = locale_from_request(request)
        workspace = _require_workspace(workspaces, workspace_id)
        form = await _verified_form(request, settings)
        parent = str(form.get("parent", "."))
        try:
            name = str(form.get("name", ""))
            directory = str(form.get("kind", "file")) == "directory"
            if is_remote(workspace):
                await remote.create(
                    workspace, parent, name, directory=directory
                )
            else:
                ensure_exposed_local_path(workspace, parent)
                files.create(workspace["path"], parent, name, directory=directory)
        except (
            ValueError,
            FileExistsError,
            OSError,
            SSHBackendError,
            RemoteAccessError,
        ) as exc:
            return RedirectResponse(
                _url_with_query(
                    f"/w/{workspace_id}/files",
                    path=parent,
                    error=_localized_exception(locale, exc),
                ),
                status_code=303,
            )
        return RedirectResponse(
            _url_with_query(f"/w/{workspace_id}/files", path=parent),
            status_code=303,
        )

    @app.post("/w/{workspace_id}/files/rename")
    async def rename_file_entry(request: Request, workspace_id: str):  # type: ignore[no-untyped-def]
        locale = locale_from_request(request)
        workspace = _require_workspace(workspaces, workspace_id)
        form = await _verified_form(request, settings)
        source = str(form.get("path", ""))
        parent = _relative_parent(source)
        try:
            new_name = str(form.get("new_name", ""))
            if is_remote(workspace):
                await remote.rename(workspace, source, new_name)
            else:
                ensure_exposed_local_path(workspace, source)
                files.rename(workspace["path"], source, new_name)
        except (
            ValueError,
            FileExistsError,
            OSError,
            SSHBackendError,
            RemoteAccessError,
        ) as exc:
            return RedirectResponse(
                _url_with_query(
                    f"/w/{workspace_id}/files",
                    path=parent,
                    error=_localized_exception(locale, exc),
                ),
                status_code=303,
            )
        return RedirectResponse(
            _url_with_query(f"/w/{workspace_id}/files", path=parent),
            status_code=303,
        )

    @app.post("/w/{workspace_id}/files/delete")
    async def delete_file_entry(request: Request, workspace_id: str):  # type: ignore[no-untyped-def]
        locale = locale_from_request(request)
        workspace = _require_workspace(workspaces, workspace_id)
        form = await _verified_form(request, settings)
        target = str(form.get("path", ""))
        parent = _relative_parent(target)
        try:
            if is_remote(workspace):
                await remote.delete(workspace, target)
            else:
                ensure_exposed_local_path(workspace, target)
                files.delete(workspace["path"], target)
        except (
            OSError,
            UnsupportedFileError,
            SSHBackendError,
            RemoteAccessError,
        ) as exc:
            return RedirectResponse(
                _url_with_query(
                    f"/w/{workspace_id}/files",
                    path=parent,
                    error=_localized_exception(locale, exc),
                ),
                status_code=303,
            )
        return RedirectResponse(
            _url_with_query(f"/w/{workspace_id}/files", path=parent),
            status_code=303,
        )

    @app.get("/w/{workspace_id}/recent", response_class=HTMLResponse)
    async def recent_page(request: Request, workspace_id: str) -> HTMLResponse:
        locale = locale_from_request(request)
        workspace = _require_workspace(workspaces, workspace_id)
        if not remote.supports_capability(workspace, "recent"):
            raise HTTPException(status_code=404, detail="Recent is not supported")
        refresh_errors: list[str] = []
        try:
            recent_scan = await recent_workspace_files(workspace)
            previous_files = recent_file_snapshots.get(workspace_id, {})
            current_files: dict[str, tuple[int, int]] = {}
            recent_file_rows: list[dict[str, Any]] = []
            for entry in recent_scan.entries:
                current_files[entry.relative_path] = (entry.size, entry.mtime_ns)
                previous = previous_files.get(entry.relative_path)
                recent_file_rows.append(
                    {
                        "name": entry.name,
                        "relative_path": entry.relative_path,
                        "size": entry.size,
                        "mtime_ns": entry.mtime_ns,
                        "growing": bool(
                            previous
                            and entry.size > previous[0]
                            and entry.mtime_ns >= previous[1]
                        ),
                    }
                )
            recent_file_snapshots[workspace_id] = current_files
            recent_file_cache[workspace_id] = (
                [dict(item) for item in recent_file_rows],
                recent_scan,
            )
        except (OSError, RemoteAccessError, SSHBackendError) as exc:
            if not is_remote(workspace):
                raise
            refresh_errors.append(_localized_exception(locale, exc))
            cached = recent_file_cache.get(workspace_id)
            if cached:
                recent_file_rows = [
                    {**item, "growing": False}
                    for item in cached[0]
                ]
                recent_scan = cached[1]
            else:
                recent_file_rows = []
                recent_scan = RecentFiles(entries=[], scanned_files=0, truncated=False)

        try:
            terminal_list = await ensure_terminal_list(workspace)
        except (OSError, RemoteAccessError, SSHBackendError) as exc:
            if not is_remote(workspace):
                raise
            refresh_errors.append(_localized_exception(locale, exc))
            terminal_list = store.list_terminals(workspace_id)
        for item in terminal_list:
            item["last_output_label"] = (
                _relative_time(item["last_output_at"], locale)
                if item.get("last_output_at")
                else translate(locale, "terminal.no_output")
            )
            item["last_opened_label"] = _relative_time(item["last_opened_at"], locale)
        store.touch_workspace(workspace_id, tab="recent")
        return templates.TemplateResponse(
            request=request,
            name="recent.html",
            context=_workspace_context(
                settings,
                workspace,
                active_tab="recent",
                workspace_commands_supported=remote.supports_capability(
                    workspace, "workspace_command"
                ),
                recent_supported=remote.supports_capability(workspace, "recent"),
                recent_files=recent_file_rows,
                recent_scan=recent_scan,
                terminals=terminal_list,
                refresh_error=" · ".join(dict.fromkeys(refresh_errors)),
                format_size=_format_size,
                format_time_ns=lambda value: _relative_time_ns(value, locale),
                **_workspace_status(store, terminals, workspace),
            ),
        )

    @app.websocket("/ws/terminal/{terminal_id}")
    async def terminal_socket(websocket: WebSocket, terminal_id: str) -> None:
        async def reject(code: int, reason: str = "") -> None:
            await websocket.accept()
            await websocket.close(code=code, reason=reason)

        if not _valid_websocket_origin(websocket):
            await reject(4403, "Origin rejected")
            return
        session = auth.websocket_session(websocket)
        if not session:
            await reject(4401, "Authentication required")
            return
        device_id = str(session["id"])
        device_sockets = active_websockets.get(device_id, [])
        if len(device_sockets) >= 8 or sum(map(len, active_websockets.values())) >= 64:
            await reject(4429, "Too many terminal connections")
            return
        terminal = store.get_terminal(terminal_id)
        if not terminal:
            await reject(4404, "Terminal not found")
            return
        try:
            workspace = _require_workspace(workspaces, str(terminal["workspace_id"]))
        except (KeyError, HTTPException):
            await reject(4404, "Workspace not found")
            return
        await websocket.accept()
        active_websockets.setdefault(device_id, []).append(websocket)
        active_terminal_websockets.setdefault(terminal_id, []).append(websocket)
        bridge_task: asyncio.Task[None] | None = None
        expiry_task: asyncio.Task[None] | None = None
        try:
            if is_remote(workspace):
                bridge_task = asyncio.create_task(
                    remote.bridge(websocket, workspace, terminal, device_id=device_id)
                )
            else:
                bridge_task = asyncio.create_task(
                    terminals.bridge(websocket, workspace, terminal, device_id=device_id)
                )
            expiry_task = asyncio.create_task(
                asyncio.sleep(max(0, int(session.get("expires_in", 0))))
            )
            done, _ = await asyncio.wait(
                {bridge_task, expiry_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if expiry_task in done and not bridge_task.done():
                await _close_websocket(
                    websocket, code=4401, reason="Session expired"
                )
                bridge_task.cancel()
            try:
                await bridge_task
            except (SSHBackendError, RemoteAccessError, TerminalError, OSError):
                await _close_websocket(
                    websocket,
                    code=1013,
                    reason="Terminal backend temporarily unavailable",
                )
            except (WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
                pass
        finally:
            tasks = tuple(
                task for task in (bridge_task, expiry_task) if task is not None
            )
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            sockets = active_websockets.get(device_id, [])
            with contextlib.suppress(ValueError):
                sockets.remove(websocket)
            if not sockets:
                active_websockets.pop(device_id, None)
            terminal_sockets = active_terminal_websockets.get(terminal_id, [])
            with contextlib.suppress(ValueError):
                terminal_sockets.remove(websocket)
            if not terminal_sockets:
                active_terminal_websockets.pop(terminal_id, None)

    return app


def _context(settings: Settings, **values: Any) -> dict[str, Any]:
    access_scope_label, access_scope_class = _access_scope(settings)
    return {
        "csrf_token": settings.csrf_token,
        "root_path": settings.root,
        "access_scope_label": access_scope_label,
        "access_scope_class": access_scope_class,
        **values,
    }


def _access_scope(settings: Settings) -> tuple[str, str]:
    host = settings.host.strip().casefold()
    if host in {"127.0.0.1", "localhost", "::1"}:
        return "app.local_only", "local"
    if host in {"0.0.0.0", "::"}:
        return "app.network_exposed", "network"
    return f"{settings.host}:{settings.port}", "network"


def _node_pairing_reachability(request: Request) -> dict[str, str | bool | None]:
    core_url = str(request.base_url).rstrip("/")
    parsed = urlparse(core_url)
    hostname = str(parsed.hostname or "").casefold()
    loopback = hostname == "localhost" or hostname.endswith(".localhost")
    if hostname and not loopback:
        with contextlib.suppress(ValueError):
            loopback = ipaddress.ip_address(hostname).is_loopback
    issue_key: str | None = None
    if loopback or not hostname:
        issue_key = "node.pair.reachability_local"
    elif parsed.scheme.casefold() != "https":
        issue_key = "node.pair.reachability_https"
    return {
        "core_url": core_url,
        "node_pairing_ready": issue_key is None,
        "node_pairing_issue_key": issue_key,
    }


def _workspace_context(
    settings: Settings,
    workspace: Mapping[str, Any],
    *,
    active_tab: str,
    workspace_commands_supported: bool,
    **values: Any,
) -> dict[str, Any]:
    remote_run = workspace.get("remote_run")
    persistent_workspace = bool(
        not workspace.get("transient")
        and workspace.get("workspace_kind") == "workspace"
    )
    stored_commands = (
        StateStore.workspace_commands_from(workspace) if persistent_workspace else ()
    )
    remote_run_display_state = (
        _run_display_state(remote_run.get("state"), remote_run.get("exit_code"))
        if isinstance(remote_run, Mapping)
        else None
    )
    remote_run_workspace_source = bool(
        isinstance(remote_run, Mapping)
        and str(remote_run.get("source_kind") or "") == "workspace"
        and remote_run.get("source_workspace_id")
    )
    return _context(
        settings,
        title=workspace["display_name"],
        workspace=workspace,
        active_tab=active_tab,
        workspace_commands_visible=persistent_workspace,
        workspace_commands_supported=(
            persistent_workspace and workspace_commands_supported
        ),
        workspace_commands=[
            {
                "slot": slot,
                "command": command,
                "command_digest": workspace_command_digest(command),
                "launch_id": uuid.uuid4().hex,
            }
            for slot, command in enumerate(stored_commands)
        ],
        remote_run_display_state=remote_run_display_state,
        remote_run_source_url=(
            f"/remote-runs/{remote_run['id']}/source"
            if remote_run_workspace_source
            else None
        ),
        remote_run_retry_url=(
            _url_with_query("/remote-runs/new", retry_run_id=str(remote_run["id"]))
            if remote_run_workspace_source
            else None
        ),
        **values,
    )


def _workspace_status(
    store: StateStore,
    terminals: TerminalManager,
    workspace: Mapping[str, Any],
    *,
    session_active: bool | None = None,
) -> dict[str, Any]:
    terminal_count = len(store.list_terminals(str(workspace["id"])))
    if workspace.get("backend_kind") == "remote":
        return {
            "session_active": None,
            "terminal_count": terminal_count,
            "session_status_label": "workspace.status.remote",
            "session_status_class": "remote",
        }
    local_active = (
        terminals.session_exists(str(workspace["tmux_session"]))
        if session_active is None
        else session_active
    )
    return {
        "session_active": local_active,
        "terminal_count": terminal_count,
        "session_status_label": (
            "workspace.status.persistent" if local_active else "workspace.status.reconnect"
        ),
        "session_status_class": "running" if local_active else "",
    }


def _run_display_state(state: object, exit_code: object) -> str:
    normalized = str(state or "preparing")
    if (
        normalized == "finished"
        and isinstance(exit_code, int)
        and not isinstance(exit_code, bool)
        and exit_code != 0
    ):
        return "failed"
    return normalized


def _remote_run_status_payload(
    run: Mapping[str, Any], *, locale: str
) -> dict[str, Any]:
    workspace_id = str(run.get("workspace_id") or "")
    state = str(run.get("state", "preparing"))
    display_state = _run_display_state(state, run.get("exit_code"))
    return {
        "id": str(run.get("id", "")),
        "state": state,
        "display_state": display_state,
        "state_label": translate(locale, f"remote_run.state.{display_state}"),
        "phase": run.get("phase"),
        "exit_code": run.get("exit_code"),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "stop_requested_at": run.get("stop_requested_at"),
        "ended_at": run.get("ended_at"),
        "expires_at": run.get("expires_at"),
        "error_code": run.get("error_code"),
        "error_detail": _localized_remote_run_error_detail(locale, run),
        "connection": run.get("connection", "online"),
        "cleanup_pending": bool(run.get("cleanup_pending", False)),
        "workspace_id": workspace_id or None,
        "workspace_url": f"/w/{workspace_id}/terminal" if workspace_id else None,
    }


def _file_run_editor_url(
    run: Mapping[str, Any],
    *,
    file_run_error: str | None = None,
) -> str:
    path = quote(str(run.get("relative_path") or ""), safe="/")
    return _url_with_query(
        f"/w/{run['workspace_id']}/edit/{path}",
        run=run.get("id"),
        file_run_error=file_run_error,
    )


def _file_run_terminal_url(
    store: StateStore, run: Mapping[str, Any]
) -> str | None:
    terminal_id = str(run.get("terminal_id") or "")
    if not terminal_id:
        return None
    terminal = store.get_terminal(terminal_id)
    if (
        terminal is None
        or str(terminal.get("workspace_id")) != str(run.get("workspace_id"))
        or str(terminal.get("role") or "shell") != "file_run"
        or str(terminal.get("managed_run_id") or "") != str(run.get("id"))
    ):
        return None
    return _url_with_query(
        f"/w/{run['workspace_id']}/terminal", terminal=terminal_id
    )


def _file_run_destination(
    store: StateStore,
    run: Mapping[str, Any],
    *,
    prefer_terminal: bool,
) -> str:
    terminal_url = _file_run_terminal_url(store, run)
    if prefer_terminal and terminal_url:
        return terminal_url
    return _file_run_editor_url(run)


def _file_run_duration_seconds(run: Mapping[str, Any]) -> int | None:
    started_at = run.get("started_at")
    if not started_at:
        return None
    ended_at = run.get("ended_at")
    try:
        started = datetime.fromisoformat(str(started_at))
        ended = datetime.fromisoformat(str(ended_at)) if ended_at else datetime.now(UTC)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    if ended.tzinfo is None:
        ended = ended.replace(tzinfo=UTC)
    return max(0, int((ended - started).total_seconds()))


def _localized_file_run_error_code(locale: str, code: Any) -> str | None:
    key = FILE_RUN_ERROR_KEYS.get(str(code or ""))
    return translate(locale, key) if key else None


def _localized_file_run_exception(locale: str, exc: BaseException) -> str:
    if isinstance(exc, FileRunConflict):
        active = exc.values.get("active_run") or {}
        return translate(
            locale,
            "file_run.error.slot_occupied",
            file=str(active.get("relative_path") or translate(locale, "common.file")),
        )
    coded = _localized_file_run_error_code(locale, getattr(exc, "code", None))
    if coded:
        return coded
    if isinstance(exc, FileConflictError):
        return translate(locale, "file_run.error.source_changed")
    localized = _localized_exception(locale, exc)
    if localized != str(exc):
        return localized
    return translate(locale, "file_run.error.start_failed")


def _file_run_status_payload(
    store: StateStore,
    run: Mapping[str, Any],
    *,
    locale: str,
) -> dict[str, Any]:
    state = str(run.get("state") or "preparing")
    display_state = _run_display_state(state, run.get("exit_code"))
    duration_seconds = _file_run_duration_seconds(run)
    terminal_url = _file_run_terminal_url(store, run)
    return {
        "id": str(run.get("id") or ""),
        "workspace_id": str(run.get("workspace_id") or ""),
        "relative_path": str(run.get("relative_path") or ""),
        "runner_id": str(run.get("runner_id") or ""),
        "state": state,
        "display_state": display_state,
        "state_label": translate(locale, f"file_run.state.{display_state}"),
        "exit_code": run.get("exit_code"),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "stop_requested_at": run.get("stop_requested_at"),
        "ended_at": run.get("ended_at"),
        "duration_seconds": duration_seconds,
        "error_code": run.get("error_code"),
        "error_detail": _localized_file_run_error_code(
            locale, run.get("error_code")
        ),
        "connection": str(run.get("connection") or "online"),
        "active": state in {"preparing", "running"},
        "needs_force": bool(
            state in {"preparing", "running"} and run.get("stop_requested_at")
        ),
        "editor_url": _file_run_editor_url(run),
        "terminal_url": terminal_url,
        "status_url": f"/api/file-runs/{run['id']}/status",
        "stop_url": f"/file-runs/{run['id']}/stop",
        "kill_url": f"/file-runs/{run['id']}/kill",
    }


def _activity_event_view(
    event: Mapping[str, Any],
    *,
    locale: str,
) -> dict[str, Any]:
    kind = str(event.get("kind") or "")
    primary = str(event.get("current_primary_label") or event.get("primary_label") or "")
    secondary = str(
        event.get("current_secondary_label") or event.get("secondary_label") or ""
    )
    exit_code = event.get("exit_code")
    file_event = event.get("subject_type") == "file_run"
    prefix = "activity.file_run" if file_event else "activity.remote_run"
    if kind.endswith(".completed"):
        title_key = f"{prefix}.completed"
        message_key = f"{prefix}.completed_copy"
        status_class = "completed"
    elif kind.endswith(".failed"):
        title_key = f"{prefix}.failed"
        message_key = (
            f"{prefix}.failed_exit_copy"
            if exit_code is not None
            else f"{prefix}.failed_start_copy"
        )
        status_class = "failed"
    elif kind.endswith(".stopped"):
        title_key = f"{prefix}.stopped"
        message_key = f"{prefix}.stopped_copy"
        status_class = "stopped"
    else:
        title_key = f"{prefix}.attention"
        message_key = f"{prefix}.attention_copy"
        status_class = "attention"
    values = {
        "source": primary,
        "remote": secondary,
        "file": primary,
        "workspace": secondary,
        "code": exit_code if exit_code is not None else "—",
    }
    subject_exists = bool(event.get("subject_exists"))
    target_url = None
    terminal_url = None
    if subject_exists and event.get("subject_type") == "remote_run":
        target_url = f"/remote-runs/{event['subject_id']}"
    elif subject_exists and file_event:
        workspace_id = str(event.get("current_workspace_id") or "")
        relative_path = quote(
            str(event.get("current_relative_path") or ""), safe="/"
        )
        target_url = _url_with_query(
            f"/w/{workspace_id}/edit/{relative_path}",
            run=event["subject_id"],
        )
        terminal_id = str(event.get("current_terminal_id") or "")
        if terminal_id:
            terminal_url = _url_with_query(
                f"/w/{workspace_id}/terminal", terminal=terminal_id
            )
    return {
        **dict(event),
        "source_label": primary,
        "remote_label": secondary,
        "title": translate(locale, title_key),
        "summary": translate(locale, message_key, **values),
        "occurred_label": _relative_time(str(event.get("occurred_at") or ""), locale),
        "status_class": status_class,
        "target_url": target_url,
        "terminal_url": terminal_url,
        "subject_exists": subject_exists,
        "unread": event.get("read_at") is None,
    }


def _activity_notification_payload(
    event: Mapping[str, Any],
    *,
    locale: str,
) -> dict[str, Any]:
    view = _activity_event_view(event, locale=locale)
    return {
        "id": str(view["id"]),
        "kind": str(view["kind"]),
        "title": str(view["title"]),
        "body": str(view["summary"]),
        "url": view["target_url"],
    }


def _relative_time_ns(value: int, locale: str) -> str:
    if not value:
        return "—"
    return _relative_time(
        datetime.fromtimestamp(value / 1_000_000_000, tz=UTC).isoformat(), locale
    )


def _format_size(size: int) -> str:
    value = float(max(0, size))
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}" if value < 10 else f"{value:.0f} {unit}"
        value /= 1024
    return f"{size} B"


def _file_view_kind(path: str, content_type: str) -> str:
    suffix = Path(path).suffix.casefold()
    if content_type in {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/avif",
    }:
        return "image"
    if content_type == "application/pdf":
        return "pdf"
    if suffix == ".json" or content_type == "application/json":
        return "json"
    if suffix == ".csv" or content_type == "text/csv":
        return "csv"
    if content_type.startswith("text/") or suffix in {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".css",
        ".html",
        ".htm",
        ".md",
        ".toml",
        ".yaml",
        ".yml",
        ".ini",
        ".cfg",
        ".conf",
        ".env",
        ".log",
        ".sql",
        ".sh",
        ".zsh",
        ".fish",
        ".txt",
        ".xml",
    }:
        return "text"
    return "binary"


def _inline_media_too_large(content_type: str, size: int) -> bool:
    if content_type.startswith("image/"):
        return size > MAX_INLINE_IMAGE_BYTES
    if content_type == "application/pdf":
        return size > MAX_INLINE_PDF_BYTES
    return False


def _is_sensitive_file_name(name: str) -> bool:
    lowered = name.casefold()
    if lowered == ".env" or (
        lowered.startswith(".env.") and lowered != ".env.example"
    ):
        return True
    if lowered in {
        "id_rsa",
        "id_ed25519",
        "credentials.json",
        "service-account.json",
    }:
        return True
    return lowered.endswith((".pem", ".key", ".p12", ".pfx"))


async def _store_upload(
    upload: UploadFile,
    target: Path,
    max_bytes: int,
    *,
    locale: str = "ko",
) -> None:
    existing_mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
    temp_path: str | None = None
    total = 0
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=target.parent, prefix=f".{target.name}.upload-", delete=False
        ) as temporary:
            temp_path = temporary.name
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(
                        translate(
                            locale,
                            "files.error.too_large",
                            size=_format_size(max_bytes),
                        )
                    )
                temporary.write(chunk)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temp_path, existing_mode)
        os.replace(temp_path, target)
        temp_path = None
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


async def _store_request_upload(
    request: Request,
    target: Path,
    max_bytes: int,
    *,
    overwrite: bool,
) -> None:
    existing_mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
    temp_path: str | None = None
    total = 0
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=target.parent, prefix=f".{target.name}.upload-", delete=False
        ) as temporary:
            temp_path = temporary.name
            async for chunk in request.stream():
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("Upload exceeds the configured size limit")
                temporary.write(chunk)
            temporary.flush()
            os.fsync(temporary.fileno())
        if not overwrite and target.exists():
            raise FileExistsError(target.name)
        os.chmod(temp_path, existing_mode)
        os.replace(temp_path, target)
        temp_path = None
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def _relative_time(value: str, locale: str = "ko") -> str:
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError:
        return translate(locale, "time.recent")
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    seconds = max(0, int((datetime.now(UTC) - timestamp.astimezone(UTC)).total_seconds()))
    if seconds < 60:
        return translate(locale, "time.just_now")
    minutes = seconds // 60
    if minutes < 60:
        return translate(locale, "time.minutes_ago", count=minutes)
    hours = minutes // 60
    if hours < 24:
        return translate(locale, "time.hours_ago", count=hours)
    days = hours // 24
    if days < 7:
        if days == 1:
            return translate(locale, "time.day_ago")
        return translate(locale, "time.days_ago", count=days)
    return timestamp.astimezone(UTC).strftime("%Y-%m-%d")


def _require_workspace(manager: WorkspaceManager, workspace_id: str) -> dict[str, Any]:
    try:
        workspace = manager.require(workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Workspace not found") from exc
    if (
        not manager.allow_local_workspaces
        and workspace.get("backend_kind", "local") != "remote"
    ):
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace


def _require_terminal(
    store: StateStore, workspace_id: str, terminal_id: str
) -> dict[str, Any]:
    terminal = store.get_terminal(terminal_id)
    if not terminal or terminal["workspace_id"] != workspace_id:
        raise HTTPException(status_code=404, detail="Terminal not found")
    return terminal


async def _verified_form(
    request: Request,
    settings: Settings,
    *,
    max_part_size: int = 1024 * 1024,
):  # type: ignore[no-untyped-def]
    form = await request.form(max_part_size=max_part_size)
    supplied = str(form.get("_csrf", ""))
    if not secure_compare(supplied, settings.csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    return form


def _verified_csrf_header(request: Request, settings: Settings) -> None:
    supplied = request.headers.get("x-termroom-csrf", "")
    if not secure_compare(supplied, settings.csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def _valid_websocket_origin(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    if not origin:
        return False
    origin_parts = urlparse(origin)
    return origin_parts.netloc == websocket.headers.get("host") and origin_parts.scheme in {
        "http",
        "https",
    }


async def _close_websocket(websocket: WebSocket, *, code: int, reason: str) -> None:
    with contextlib.suppress(WebSocketDisconnect, RuntimeError):
        await websocket.close(code=code, reason=reason)


def _breadcrumbs(relative_path: str) -> list[dict[str, str]]:
    crumbs = [{"name": "root", "path": "."}]
    if relative_path == ".":
        return crumbs
    current = Path()
    for part in Path(relative_path).parts:
        current /= part
        crumbs.append({"name": part, "path": current.as_posix()})
    return crumbs


def _local_location_picker(
    requested_path: str | None,
    *,
    show_hidden: bool = False,
) -> dict[str, Any]:
    requested = Path(requested_path).expanduser() if requested_path else Path.home()
    if not requested.is_absolute():
        raise ValueError("Folder browser path must be absolute")
    current = requested.resolve(strict=True)
    if not current.is_dir():
        raise NotADirectoryError(current)

    hidden_count = 0
    entries: list[dict[str, str]] = []
    for child in current.iterdir():
        try:
            if child.is_symlink() or not child.is_dir():
                continue
            resolved_child = child.resolve(strict=True)
        except OSError:
            continue
        if child.name.startswith("."):
            hidden_count += 1
            if not show_hidden:
                continue
        entries.append({"name": child.name, "path": str(resolved_child)})
    entries.sort(key=lambda item: item["name"].casefold())
    parent = None if current.parent == current else str(current.parent)
    return {
        "current": str(current),
        "parent": parent,
        "entries": entries,
        "hidden_count": hidden_count,
        "show_hidden": show_hidden,
    }


def _workspace_relative(workspace_path: Path, path: Path) -> str:
    relative = path.resolve(strict=True).relative_to(workspace_path.resolve(strict=True))
    return "." if str(relative) == "." else relative.as_posix()


def _normalize_relative_path(value: str) -> str:
    path = PurePosixPath(value or ".")
    if path.is_absolute() or ".." in path.parts:
        raise PathBoundaryError("Path escapes the Workspace")
    normalized = PurePosixPath(*[part for part in path.parts if part not in {"", "."}])
    text = normalized.as_posix()
    return "." if text in {"", "."} else text


def _relative_parent(value: str) -> str:
    normalized = _normalize_relative_path(value)
    if normalized == ".":
        return "."
    parent = PurePosixPath(normalized).parent.as_posix()
    return "." if parent in {"", "."} else parent


def _is_internal_state_entry(
    settings: Settings,
    workspace: Mapping[str, Any],
    relative_directory: str,
    entry_name: str,
) -> bool:
    if workspace.get("backend_kind") == "remote":
        return False
    workspace_root = Path(workspace["path"]).resolve(strict=True)
    current = resolve_inside(
        workspace_root,
        relative_directory,
        must_exist=True,
    )
    try:
        state_dir = settings.state_dir.resolve(strict=True)
    except OSError:
        return False
    return state_dir.parent == current and state_dir.name == entry_name


def _file_browser_entry_is_noise(entry: Any) -> bool:
    name = str(entry.name)
    return (
        name in DEFAULT_FILE_BROWSER_NOISE
        or (bool(entry.is_dir) and name.startswith("."))
        or (
            not bool(entry.is_dir)
            and name.startswith(".")
            and name.endswith((".swp", ".swo", ".swn", ".swm", ".swl", ".swk"))
        )
    )


def _normalize_upload_filename(value: str) -> str:
    name = PurePosixPath(value).name
    if not value or value != name or "/" in value or "\\" in value:
        raise ValueError("Invalid upload filename")
    return value


def _content_disposition(filename: str, disposition: str) -> str:
    safe_disposition = "inline" if disposition == "inline" else "attachment"
    try:
        filename.encode("ascii")
    except UnicodeEncodeError:
        fallback = "download"
    else:
        fallback = "".join(
            character
            if 0x20 <= ord(character) < 0x7F and character not in {'"', "\\"}
            else "_"
            for character in filename
        ) or "download"
    encoded = quote(filename, safe="")
    return f'{safe_disposition}; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


def _parse_single_byte_range(value: str | None, size: int) -> tuple[int, int] | None:
    if not value or not value.startswith("bytes=") or size <= 0:
        return None
    specification = value[6:].strip()
    if not specification or "," in specification or "-" not in specification:
        return None
    start_text, end_text = specification.split("-", 1)
    try:
        if not start_text:
            suffix = int(end_text)
            if suffix <= 0:
                return None
            start = max(0, size - suffix)
            return start, size - 1
        start = int(start_text)
        if start < 0 or start >= size:
            return None
        end = size - 1 if not end_text else int(end_text)
        if end < start:
            return None
        return start, min(end, size - 1)
    except ValueError:
        return None


def _ssh_form_values(form: Any) -> dict[str, str]:
    return {
        "target": str(form.get("target", "")).strip(),
        "name": str(form.get("name", "")).strip(),
        "username": str(form.get("username", "")).strip(),
        "port": str(form.get("port", "")).strip(),
        "identity_file": str(form.get("identity_file", "")).strip(),
        "auth_mode": str(form.get("auth_mode", "password")).strip() or "password",
    }


def _apply_ssh_overrides(
    target: dict[str, Any],
    values: Mapping[str, str],
    *,
    locale: str = "ko",
) -> None:
    if values.get("username"):
        target["username"] = values["username"]
    if values.get("port"):
        try:
            port = int(values["port"])
        except ValueError as exc:
            raise ValueError(translate(locale, "ssh.error.port_number")) from exc
        if not 1 <= port <= 65535:
            raise ValueError(translate(locale, "ssh.error.port_range"))
        target["port"] = port
    if values.get("auth_mode") == "existing" and values.get("identity_file"):
        try:
            identity_file = SSHBackend.validate_identity_file(values["identity_file"])
        except SSHBackendError as exc:
            raise ValueError(str(exc)) from exc
        target["identity_file"] = identity_file
        target["identity_files"] = (identity_file,)
    if not str(target.get("username") or "").strip():
        raise ValueError(translate(locale, "ssh.error.username_required"))


def _require_computer(store: StateStore, computer_id: str) -> dict[str, Any]:
    computer = store.get_computer(computer_id)
    if not computer:
        raise HTTPException(status_code=404, detail="Computer not found")
    return computer


def _remote_connection_view(
    computer: Mapping[str, Any],
    *,
    remote: RemoteAccess,
    locale: str,
) -> dict[str, Any]:
    """Return the small current-state view shared by SSH and Node surfaces."""

    method = str(computer.get("connection_method") or "ssh")
    live = False
    if method == "node" and computer.get("node_revoked_at") is None:
        with contextlib.suppress(KeyError, ValueError):
            live = remote.status(computer)["online"] is True
    last_success_at = str(
        computer.get("last_seen_at") or computer.get("last_connected_at") or ""
    )
    recently_successful = False
    if last_success_at:
        with contextlib.suppress(ValueError):
            last_success = datetime.fromisoformat(last_success_at)
            if last_success.tzinfo is None:
                last_success = last_success.replace(tzinfo=UTC)
            age = datetime.now(UTC) - last_success.astimezone(UTC)
            recently_successful = timedelta(0) <= age <= _REMOTE_STATUS_FRESH_FOR
    if computer.get("node_revoked_at") is not None:
        state = "unavailable"
    elif live:
        state = "available"
    elif method == "node":
        state = (
            "unavailable"
            if last_success_at or computer.get("last_error")
            else "unchecked"
        )
    elif computer.get("last_error"):
        state = "unavailable"
    elif recently_successful:
        state = "available"
    else:
        state = "unchecked"
    return {
        "state": state,
        "label": translate(locale, f"remote.status.{state}"),
        "last_success_at": last_success_at or None,
        "last_success_label": _relative_time(last_success_at, locale)
        if last_success_at
        else None,
    }


def _query_message(exc: BaseException) -> str:
    return urlencode({"message": str(exc)})[8:]


def _upload_error_status(exc: BaseException) -> int:
    if isinstance(exc, FileExistsError):
        return 409
    if isinstance(exc, PathBoundaryError):
        return 403
    if isinstance(exc, PermissionError):
        return 403
    if isinstance(exc, SSHBackendError):
        return 502
    if isinstance(exc, OSError) and exc.errno == 28:
        return 507
    if isinstance(exc, ValueError) and str(exc) == "Upload exceeds the configured size limit":
        return 413
    return 400


def _url_with_query(base_url: str, **values: Any) -> str:
    query = urlencode({key: value for key, value in values.items() if value is not None})
    return f"{base_url}?{query}" if query else base_url


def _localized_exception(locale: str, exc: BaseException) -> str:
    return localize_exception(locale, exc)


def _localized_remote_run_exception(locale: str, exc: BaseException) -> str:
    localized = localize_exception(locale, exc)
    if localized != str(exc):
        return localized
    return translate(locale, "remote_run.error.prepare_failed")


def _localized_result_collection_exception(
    locale: str, exc: ResultCollectionError
) -> str:
    key = RESULT_COLLECTION_ERROR_KEYS.get(
        exc.code, "remote_run.collect.error.unavailable"
    )
    return translate(locale, key)


def _remote_run_collection_view(value: Any, locale: str) -> dict[str, Any]:
    payload = value.as_dict()
    for item in payload["items"]:
        change = str(item["change"])
        item["change_label"] = translate(
            locale, f"remote_run.collect.change.{change}"
        )
        state_field = "outcome" if "outcome" in item else "status"
        state = str(item[state_field])
        item[f"{state_field}_label"] = translate(
            locale, f"remote_run.collect.{state_field}.{state}"
        )
        reason_key = RESULT_COLLECTION_REASON_KEYS.get(
            str(item["reason"]), "remote_run.collect.reason.source_conflict"
        )
        item["reason_label"] = translate(locale, reason_key)
    return payload


def _remote_run_collection_result_query(request: Request) -> dict[str, int] | None:
    if request.query_params.get("collected") != "1":
        return None
    result: dict[str, int] = {}
    for key in ("applied", "conflict", "already_result", "skipped", "failed"):
        try:
            value = int(request.query_params.get(key, "0"))
        except ValueError:
            value = 0
        result[key] = min(max(value, 0), 1_000_000_000)
    return result


def _localized_remote_run_error_detail(
    locale: str, run: Mapping[str, Any]
) -> str | None:
    if not run.get("error_detail"):
        return None
    localized = localize_error_code(locale, run.get("error_code"))
    return localized or translate(locale, "remote_run.error.prepare_failed")


def _localized_stored_ssh_error(locale: str, value: Any) -> str | None:
    raw = str(value or "")
    if not raw:
        return None
    prefix = "termroom-i18n:"
    if not raw.startswith(prefix):
        return raw
    try:
        payload = json.loads(raw[len(prefix) :])
        key = str(payload["key"])
        values = payload.get("values") or {}
        if not isinstance(values, dict):
            return raw
        return translate(locale, key, **values)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return raw


def _error_page(request: Request, message: str, status_code: int) -> HTMLResponse:
    locale = locale_from_request(request)
    return templates.TemplateResponse(
        request=request,
        name="error.html",
        context={
            "title": translate(locale, "title.error"),
            "message": message,
            **_context(request.app.state.settings),
        },
        status_code=status_code,
    )
