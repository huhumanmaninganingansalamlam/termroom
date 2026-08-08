from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import json
import os
import tempfile
import zipfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlencode, urlparse

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
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
from starlette.datastructures import UploadFile

from termroom.auth import AuthManager, AuthRateLimited
from termroom.config import Settings
from termroom.db import StateStore
from termroom.files import (
    DEFAULT_FILE_BROWSER_NOISE,
    FileConflictError,
    FileService,
    FileSnapshot,
    RecentFiles,
    UnsupportedFileError,
)
from termroom.i18n import (
    LOCALE_COOKIE,
    SUPPORTED_LOCALES,
    locale_from_request,
    localize_exception,
    normalize_locale,
    template_context,
    translate,
)
from termroom.pwa_icon import termroom_png_icon
from termroom.runtime import runtime_stamp
from termroom.security import PathBoundaryError, is_within, resolve_inside, secure_compare
from termroom.ssh_backend import SSHBackend, SSHBackendError
from termroom.terminal_control import TerminalControl
from termroom.terminals import TerminalError, TerminalManager
from termroom.workspaces import RootManager, WorkspaceManager

PACKAGE_ROOT = Path(__file__).resolve().parent
FILE_BROWSER_PAGE_SIZE = 200
MAX_INLINE_IMAGE_BYTES = 25 * 1024 * 1024
MAX_INLINE_PDF_BYTES = 100 * 1024 * 1024
templates = Jinja2Templates(
    directory=PACKAGE_ROOT / "templates",
    context_processors=[template_context],
)


def create_app(settings: Settings) -> FastAPI:
    if not settings.login_password:
        raise ValueError(
            "Termroom login password is not configured. Add `TERMROOM_PASSWORD=...` "
            f"to {settings.state_dir / '.env'} or the environment."
        )
    store = StateStore(settings.database_path)
    store.initialize()
    roots = RootManager(settings.root)
    workspaces = WorkspaceManager(roots, store)
    files = FileService(settings.max_edit_bytes)
    terminal_control = TerminalControl()
    terminals = TerminalManager(store, terminal_control)
    ssh = SSHBackend(store, settings.state_dir, terminal_control)
    auth = AuthManager(settings)
    active_websockets: dict[str, list[WebSocket]] = {}
    active_terminal_websockets: dict[str, list[WebSocket]] = {}
    recent_file_snapshots: dict[str, dict[str, tuple[int, int]]] = {}
    recent_file_cache: dict[str, tuple[list[dict[str, Any]], RecentFiles]] = {}

    app = FastAPI(title="Termroom", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.store = store
    app.state.roots = roots
    app.state.workspaces = workspaces
    app.state.files = files
    app.state.terminals = terminals
    app.state.terminal_control = terminal_control
    app.state.ssh = ssh
    app.state.auth = auth
    app.state.runtime_stamp = runtime_stamp()
    app.mount("/static", StaticFiles(directory=PACKAGE_ROOT / "static"), name="static")

    @app.middleware("http")
    async def reject_mixed_runtime(request: Request, call_next):  # type: ignore[no-untyped-def]
        if (
            request.url.path not in {"/health", "/sw.js"}
            and not request.url.path.startswith(("/static/", "/icons/"))
            and runtime_stamp() != app.state.runtime_stamp
        ):
            return HTMLResponse(
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>Termroom update</title></head><body>"
                "<main style='max-width:680px;margin:12vh auto;padding:24px;"
                "font-family:system-ui,sans-serif;line-height:1.6'>"
                "<h1>Termroom was updated</h1>"
                "<p>The running Core is using older code. Run <code>termroom .</code> "
                "once in a terminal to restart the Core, then refresh this page.</p>"
                "</main></body></html>",
                status_code=503,
                headers={"X-Termroom-Restart-Required": "1"},
            )
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if not request.url.path.startswith("/static/") and request.url.path != "/health":
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    def is_remote(workspace: Mapping[str, Any]) -> bool:
        return workspace.get("backend_kind") == "ssh"

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
            return await asyncio.to_thread(ssh.ensure_workspace, workspace)
        return terminals.ensure_workspace(workspace)

    async def list_workspace_dir(
        workspace: dict[str, Any], relative_path: str
    ) -> tuple[Any, list[Any]]:
        if is_remote(workspace):
            return await asyncio.to_thread(ssh.list_dir, workspace, relative_path)
        ensure_exposed_local_path(workspace, relative_path)
        return files.list_dir(workspace["path"], relative_path)

    async def stat_workspace_file(workspace: dict[str, Any], relative_path: str):  # type: ignore[no-untyped-def]
        if is_remote(workspace):
            return await asyncio.to_thread(ssh.stat, workspace, relative_path)
        ensure_exposed_local_path(workspace, relative_path)
        return files.stat(workspace["path"], relative_path)

    async def read_workspace_text(workspace: dict[str, Any], relative_path: str):  # type: ignore[no-untyped-def]
        if is_remote(workspace):
            return await asyncio.to_thread(
                ssh.read_text, workspace, relative_path, settings.max_edit_bytes
            )
        ensure_exposed_local_path(workspace, relative_path)
        return files.read_text(workspace["path"], relative_path)

    async def read_workspace_preview(
        workspace: dict[str, Any], relative_path: str, mode: str, offset: int = 0
    ):  # type: ignore[no-untyped-def]
        if is_remote(workspace):
            return await asyncio.to_thread(
                ssh.read_text_preview,
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
            return await asyncio.to_thread(ssh.recent_files, workspace)
        return await asyncio.to_thread(files.recent_files, workspace["path"])

    async def managed_key_display(locale: str) -> tuple[str, str | None]:
        try:
            managed_key = await asyncio.to_thread(ssh.ensure_managed_key)
        except SSHBackendError as exc:
            return "", _localized_exception(locale, exc)
        return managed_key["public_key"], None

    def workspace_content_type(workspace: dict[str, Any], relative_path: str) -> str:
        if is_remote(workspace):
            return ssh.content_type(relative_path)
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

        def write_remote_archive() -> None:
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
                        _, children = ssh.list_dir(workspace, relative_path)
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
                        for chunk in ssh.download_iter(workspace, relative_path):
                            output.write(chunk)
                    added += 1

        try:
            if is_remote(workspace):
                await asyncio.to_thread(write_remote_archive)
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
            )
        request.state.session = session
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
    async def pwa_icon(size: int) -> Response:
        try:
            content = termroom_png_icon(size)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Unsupported icon size") from exc
        return Response(
            content=content,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=604800, immutable"},
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
        recent = workspaces.list_recent()
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
        return templates.TemplateResponse(
            request=request,
            name="home.html",
            context=_context(
                settings,
                title="Termroom",
                recent=recent,
            ),
        )

    @app.get("/open", response_class=HTMLResponse)
    async def workspace_open_hub(
        request: Request, computer_removed: bool = False
    ) -> HTMLResponse:
        computers = store.list_computers()
        local_roots = store.list_local_roots()
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
                local_workspace_count=sum(
                    len(store.list_workspaces_for_root(str(root["id"])))
                    for root in local_roots
                ),
                workspace_counts={
                    str(computer["id"]): len(
                        store.list_workspaces_for_computer(str(computer["id"]))
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
    ) -> HTMLResponse:
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
        root_manager = RootManager(Path(str(selected_root["path"])))
        directory, all_entries = root_manager.list_directories(path)
        hidden_count = sum(entry.name.startswith(".") for entry in all_entries)
        entries = (
            all_entries
            if hidden
            else [entry for entry in all_entries if not entry.name.startswith(".")]
        )
        relative = root_manager.relative(directory)
        location_close_url = _url_with_query(
            "/open/local",
            root=selected_root_id,
            path=relative,
            hidden=1 if hidden else None,
        )
        local_workspaces: list[dict[str, Any]] = []
        for item in store.list_workspaces_for_root(selected_root_id):
            try:
                local_workspaces.append(workspaces.require(str(item["id"])))
            except (KeyError, OSError):
                continue
        root_rows = []
        for item in local_roots:
            value = str(item["path"])
            root_rows.append(
                {
                    **item,
                    "label": Path(value).name or value,
                    "workspace_count": len(
                        store.list_workspaces_for_root(str(item["id"]))
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
                entries=entries,
                breadcrumbs=_breadcrumbs(relative),
                show_hidden=hidden,
                hidden_count=hidden_count,
                local_workspaces=local_workspaces,
                error=error,
                browse_location=browse_location,
                location_picker=location_picker,
                location_error=location_error,
                location_close_url=location_close_url,
                has_remote_computers=bool(store.list_computers()),
            ),
        )

    @app.post("/open/local/locations")
    async def add_local_location(request: Request):  # type: ignore[no-untyped-def]
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

    @app.get("/open/{computer_id}", response_class=HTMLResponse)
    async def workspace_open_remote(
        request: Request,
        computer_id: str,
        browse: bool = False,
        browse_path: str | None = None,
        browse_hidden: bool = False,
        error: str | None = None,
        connected: bool = False,
    ) -> HTMLResponse:
        computer = _require_computer(store, computer_id)
        remote_workspaces = []
        for item in store.list_workspaces_for_computer(computer_id):
            try:
                remote_workspaces.append(workspaces.require(str(item["id"])))
            except (KeyError, OSError):
                continue
        remote_picker = None
        browse_error = None
        remote_browse_close_url = f"/open/{computer_id}"
        if browse:
            try:
                remote_picker = await asyncio.to_thread(
                    ssh.list_browse_directories,
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
            except (OSError, ValueError, SSHBackendError) as exc:
                browse_error = _localized_exception(locale_from_request(request), exc)
        return templates.TemplateResponse(
            request=request,
            name="workspace_open.html",
            context=_context(
                settings,
                title=str(computer["name"]),
                mode="remote",
                computer=computer,
                remote_workspaces=remote_workspaces,
                error=error,
                connected=connected,
                browse=browse,
                remote_picker=remote_picker,
                browse_error=browse_error,
                remote_browse_close_url=remote_browse_close_url,
                has_multiple_computers=True,
            ),
        )

    @app.get("/computers/new", response_class=HTMLResponse)
    async def new_computer_page(request: Request) -> HTMLResponse:
        locale = locale_from_request(request)
        managed_public_key, managed_key_error = await managed_key_display(locale)
        return templates.TemplateResponse(
            request=request,
            name="computer_new.html",
            context=_context(
                settings,
                title=translate(locale, "title.ssh_add"),
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
            if target.get("proxycommand"):
                raise ValueError(translate(locale, "ssh.error.proxy_unsupported"))
            host_key = await asyncio.to_thread(
                ssh.probe_host_key, str(target["host"]), int(target["port"])
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
            if target.get("proxycommand"):
                raise ValueError(translate(locale, "ssh.error.proxy_unsupported"))
            probed = await asyncio.to_thread(
                ssh.probe_host_key, str(target["host"]), int(target["port"])
            )
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
            identity_file = str(target.get("identity_file") or "")
            temporary = {
                "id": "",
                "host": str(target["host"]),
                "port": int(target["port"]),
                "username": str(target["username"]),
                "ssh_alias": "",
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
                if not identity_file:
                    raise ValueError(translate(locale, "ssh.error.existing_key_required"))
            else:
                raise ValueError(translate(locale, "ssh.error.auth_unsupported"))

            temporary["identity_file"] = identity_file
            if auth_mode != "password":
                await asyncio.to_thread(ssh.test_connection, temporary)
            computer = store.create_computer(
                name=values["name"] or values["target"],
                ssh_alias=str(target.get("ssh_alias") or "") if auth_mode == "existing" else "",
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
                    title=translate(locale, "title.ssh_add"),
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
                managed_key_path=str(ssh.managed_key_path),
            ),
        )

    @app.post("/computers/{computer_id}/test")
    async def test_computer(request: Request, computer_id: str):  # type: ignore[no-untyped-def]
        locale = locale_from_request(request)
        await _verified_form(request, settings)
        computer = _require_computer(store, computer_id)
        try:
            await asyncio.to_thread(ssh.test_connection, computer)
        except SSHBackendError as exc:
            return RedirectResponse(
                f"/computers/{computer_id}?error="
                f"{quote(_localized_exception(locale, exc))}",
                status_code=303,
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

    @app.post("/computers/{computer_id}/delete")
    async def delete_computer(request: Request, computer_id: str):  # type: ignore[no-untyped-def]
        locale = locale_from_request(request)
        await _verified_form(request, settings)
        _require_computer(store, computer_id)
        workspace_ids = [
            str(item["id"]) for item in store.list_workspaces_for_computer(computer_id)
        ]
        terminal_ids = [
            str(terminal["id"])
            for workspace_id in workspace_ids
            for terminal in store.list_terminals(workspace_id)
        ]
        try:
            await asyncio.to_thread(ssh.delete_password, computer_id)
            await asyncio.to_thread(ssh.forget_host_key, computer_id)
            store.remove_computer_registration(computer_id)
        except (OSError, ValueError, SSHBackendError) as exc:
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

    @app.post("/computers/{computer_id}/workspaces")
    async def create_remote_workspace(request: Request, computer_id: str):  # type: ignore[no-untyped-def]
        locale = locale_from_request(request)
        form = await _verified_form(request, settings)
        computer = _require_computer(store, computer_id)
        remote_path = str(form.get("path", "")).strip()
        display_name = str(form.get("display_name", "")).strip() or None
        created_workspace_id: str | None = None
        try:
            canonical = await asyncio.to_thread(
                ssh.validate_workspace_path, computer, remote_path
            )
            existing = store.find_remote_workspace(computer_id, canonical)
            workspace = workspaces.open_remote(computer_id, canonical, display_name)
            if existing is None:
                created_workspace_id = str(workspace["id"])
            await asyncio.to_thread(ssh.ensure_workspace, workspace)
        except (ValueError, SSHBackendError) as exc:
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
        form = await _verified_form(request, settings)
        root_id = str(form.get("root_id", "")).strip()
        if not root_id:
            raise HTTPException(status_code=400, detail="Local folder location is required")
        root_record = store.get_root(root_id)
        if not root_record or str(root_record["path"]).startswith("ssh://"):
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
        except SSHBackendError as exc:
            if not is_remote(workspace):
                raise
            terminal_list = store.list_terminals(workspace_id)
            if not terminal_list:
                raise
            terminal_error = _localized_exception(locale, exc)
        selected = next((item for item in terminal_list if item["id"] == terminal), None)
        selected = selected or terminal_list[0]
        store.touch_workspace(workspace_id, tab="terminal")
        return templates.TemplateResponse(
            request=request,
            name="terminal.html",
            context=_workspace_context(
                settings,
                workspace,
                active_tab="terminal",
                terminals=terminal_list,
                terminal=selected,
                commands=store.list_commands(workspace_id),
                error=terminal_error,
                current_device_id=str(getattr(request.state, "session", {}).get("id", "")),
                **_workspace_status(store, terminals, workspace),
            ),
        )

    @app.get("/api/terminals/{terminal_id}/presence", response_class=JSONResponse)
    async def terminal_presence(terminal_id: str) -> dict[str, int | str]:
        if not store.get_terminal(terminal_id):
            raise HTTPException(status_code=404, detail="Terminal not found")
        return terminal_control.presence(terminal_id)

    @app.post("/w/{workspace_id}/terminals")
    async def create_terminal(request: Request, workspace_id: str):  # type: ignore[no-untyped-def]
        form = await _verified_form(request, settings)
        workspace = _require_workspace(workspaces, workspace_id)
        name = str(form.get("name", "shell"))
        if is_remote(workspace):
            terminal = await asyncio.to_thread(ssh.create_terminal, workspace, name)
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
            if action == "rename":
                name = str(form.get("name", "shell"))
                if is_remote(workspace):
                    updated = await asyncio.to_thread(
                        ssh.rename_terminal, workspace, terminal, name
                    )
                else:
                    updated = terminals.rename_terminal(workspace, terminal, name)
                return RedirectResponse(
                    f"/w/{workspace_id}/terminal?terminal={updated['id']}", status_code=303
                )
            if action == "delete":
                if is_remote(workspace):
                    remaining = await asyncio.to_thread(
                        ssh.close_terminal, workspace, terminal
                    )
                else:
                    remaining = terminals.close_terminal(workspace, terminal)
                selected = remaining[0]
                return RedirectResponse(
                    f"/w/{workspace_id}/terminal?terminal={selected['id']}", status_code=303
                )
            raise ValueError("Unknown terminal action")
        except (ValueError, TerminalError, SSHBackendError) as exc:
            return RedirectResponse(
                f"/w/{workspace_id}/terminal?terminal={terminal_id}&error="
                f"{quote(_localized_exception(locale, exc))}",
                status_code=303,
            )

    @app.get(
        "/w/{workspace_id}/terminal/{terminal_id}/scrollback", response_class=HTMLResponse
    )
    async def terminal_scrollback(
        request: Request, workspace_id: str, terminal_id: str, recent: int = 2000
    ) -> HTMLResponse:
        workspace = _require_workspace(workspaces, workspace_id)
        terminal = _require_terminal(store, workspace_id, terminal_id)
        if is_remote(workspace):
            output = await asyncio.to_thread(
                ssh.capture_scrollback, workspace, terminal, recent
            )
        else:
            output = terminals.capture_scrollback(workspace, terminal, recent)
        return templates.TemplateResponse(
            request=request,
            name="scrollback.html",
            context=_workspace_context(
                settings,
                workspace,
                active_tab="terminal",
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
        return templates.TemplateResponse(
            request=request,
            name="files.html",
            context=_workspace_context(
                settings,
                workspace,
                active_tab="files",
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
                max_upload_bytes=settings.max_upload_bytes,
                format_size=_format_size,
                format_time_ns=lambda value: _relative_time_ns(value, locale),
                **_workspace_status(store, terminals, workspace),
            ),
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
                ssh.download_iter(workspace, file_path, offset=start, length=length),
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
                ssh.download_iter(workspace, file_path, offset=start, length=length),
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
                    await ssh.upload(
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
            if len(names) != len(set(names)):
                duplicate = next(name for name in names if names.count(name) > 1)
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
                await ssh.upload_stream(
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
        ) as exc:
            status_code = _upload_error_status(exc)
            return JSONResponse(
                {"ok": False, "error": _localized_exception(locale, exc)},
                status_code=status_code,
            )
        return JSONResponse({"ok": True, "name": filename})

    @app.get("/w/{workspace_id}/edit/{file_path:path}", response_class=HTMLResponse)
    async def editor(request: Request, workspace_id: str, file_path: str) -> HTMLResponse:
        workspace = _require_workspace(workspaces, workspace_id)
        snapshot = await read_workspace_text(workspace, file_path)
        return templates.TemplateResponse(
            request=request,
            name="editor.html",
            context=_workspace_context(
                settings,
                workspace,
                active_tab="files",
                snapshot=snapshot,
                saved=request.query_params.get("saved") == "1",
                conflict=None,
                save_error=None,
                editor_unsaved=False,
                **_workspace_status(store, terminals, workspace),
            ),
        )

    @app.post("/w/{workspace_id}/edit/{file_path:path}", response_class=HTMLResponse)
    async def save_file(request: Request, workspace_id: str, file_path: str):  # type: ignore[no-untyped-def]
        workspace = _require_workspace(workspaces, workspace_id)
        form = await _verified_form(request, settings)
        content = str(form.get("content", ""))
        expected_digest = str(form.get("digest", ""))
        expected_mtime_ns = int(str(form.get("mtime_ns", "0")))
        try:
            if is_remote(workspace):
                await asyncio.to_thread(
                    ssh.write_text,
                    workspace,
                    file_path,
                    content,
                    expected_digest=expected_digest,
                    expected_mtime_ns=expected_mtime_ns,
                    max_bytes=settings.max_edit_bytes,
                )
            else:
                ensure_exposed_local_path(workspace, file_path)
                files.write_text(
                    workspace["path"],
                    file_path,
                    content,
                    expected_digest=expected_digest,
                    expected_mtime_ns=expected_mtime_ns,
                )
        except FileConflictError as exc:
            locale = locale_from_request(request)
            current = await read_workspace_text(workspace, file_path)
            return templates.TemplateResponse(
                request=request,
                name="editor.html",
                context=_workspace_context(
                    settings,
                    workspace,
                    active_tab="files",
                    snapshot=current,
                    submitted_content=content,
                    saved=False,
                    conflict=_localized_exception(locale, exc),
                    save_error=None,
                    editor_unsaved=True,
                    **_workspace_status(store, terminals, workspace),
                ),
                status_code=409,
            )
        except (OSError, UnsupportedFileError, SSHBackendError, ValueError) as exc:
            locale = locale_from_request(request)
            try:
                current = await read_workspace_text(workspace, file_path)
            except (OSError, UnsupportedFileError, SSHBackendError, ValueError):
                current = FileSnapshot(
                    path=Path(file_path),
                    relative_path=file_path,
                    content="",
                    digest=expected_digest,
                    mtime_ns=expected_mtime_ns,
                )
            return templates.TemplateResponse(
                request=request,
                name="editor.html",
                context=_workspace_context(
                    settings,
                    workspace,
                    active_tab="files",
                    snapshot=current,
                    submitted_content=content,
                    saved=False,
                    conflict=None,
                    save_error=_localized_exception(locale, exc),
                    editor_unsaved=True,
                    **_workspace_status(store, terminals, workspace),
                ),
                status_code=502 if isinstance(exc, SSHBackendError) else 409,
            )
        return RedirectResponse(
            _url_with_query(
                f"/w/{workspace_id}/edit/{quote(file_path, safe='/')}",
                saved=1,
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
                await asyncio.to_thread(ssh.create, workspace, parent, name, directory=directory)
            else:
                ensure_exposed_local_path(workspace, parent)
                files.create(workspace["path"], parent, name, directory=directory)
        except (ValueError, FileExistsError, OSError, SSHBackendError) as exc:
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
                await asyncio.to_thread(ssh.rename, workspace, source, new_name)
            else:
                ensure_exposed_local_path(workspace, source)
                files.rename(workspace["path"], source, new_name)
        except (ValueError, FileExistsError, OSError, SSHBackendError) as exc:
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
                await asyncio.to_thread(ssh.delete, workspace, target)
            else:
                ensure_exposed_local_path(workspace, target)
                files.delete(workspace["path"], target)
        except (OSError, UnsupportedFileError, SSHBackendError) as exc:
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
        except (OSError, SSHBackendError) as exc:
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
        except (OSError, SSHBackendError) as exc:
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
            workspace = workspaces.require(terminal["workspace_id"])
        except KeyError:
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
                    ssh.bridge(websocket, workspace, terminal, device_id=device_id)
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
                with contextlib.suppress(RuntimeError):
                    await websocket.close(code=4401, reason="Session expired")
                bridge_task.cancel()
            try:
                await bridge_task
            except (SSHBackendError, TerminalError, OSError):
                with contextlib.suppress(RuntimeError):
                    await websocket.close(
                        code=1013,
                        reason="Terminal backend temporarily unavailable",
                    )
            except (WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
                pass
        finally:
            for task in (bridge_task, expiry_task):
                if task is not None and not task.done():
                    task.cancel()
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


def _workspace_context(
    settings: Settings,
    workspace: Mapping[str, Any],
    *,
    active_tab: str,
    **values: Any,
) -> dict[str, Any]:
    return _context(
        settings,
        title=workspace["display_name"],
        workspace=workspace,
        active_tab=active_tab,
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
    if workspace.get("backend_kind") == "ssh":
        return {
            "session_active": None,
            "terminal_count": terminal_count,
            "session_status_label": "workspace.status.ssh",
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
        return manager.require(workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Workspace not found") from exc


def _require_terminal(
    store: StateStore, workspace_id: str, terminal_id: str
) -> dict[str, Any]:
    terminal = store.get_terminal(terminal_id)
    if not terminal or terminal["workspace_id"] != workspace_id:
        raise HTTPException(status_code=404, detail="Terminal not found")
    return terminal


async def _verified_form(request: Request, settings: Settings):  # type: ignore[no-untyped-def]
    form = await request.form()
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
    if workspace.get("backend_kind") == "ssh":
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
    return entry.name in DEFAULT_FILE_BROWSER_NOISE or (
        bool(entry.is_dir) and entry.name.startswith(".")
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
            target["identity_file"] = SSHBackend.validate_identity_file(values["identity_file"])
        except SSHBackendError as exc:
            raise ValueError(str(exc)) from exc
    if not str(target.get("username") or "").strip():
        raise ValueError(translate(locale, "ssh.error.username_required"))


def _require_computer(store: StateStore, computer_id: str) -> dict[str, Any]:
    computer = store.get_computer(computer_id)
    if not computer:
        raise HTTPException(status_code=404, detail="Computer not found")
    return computer


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
