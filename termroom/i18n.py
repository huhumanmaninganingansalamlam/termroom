from __future__ import annotations

import errno
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import Request

LOCALES_DIR = Path(__file__).resolve().parent / "locales"
SUPPORTED_LOCALES = ("en", "ko")
DEFAULT_LOCALE = "en"
LOCALE_COOKIE = "termroom_locale"

ERROR_MESSAGE_KEYS = {
    "Only regular files can be edited": "error.file.regular_edit",
    "File exceeds the editable size limit": "error.file.edit_limit",
    "Binary files cannot be edited": "error.file.binary_edit",
    "Only UTF-8 text files can be edited": "error.file.utf8_edit",
    "Symbolic links are not exposed": "error.file.symlink_hidden",
    "Only regular files can be opened": "error.file.regular_open",
    "Preview mode must be head, tail, or range": "error.file.preview_mode",
    "Binary files cannot be shown as text": "error.file.binary_preview",
    "Only UTF-8 text can be previewed": "error.file.utf8_preview",
    "Content exceeds the editable size limit": "error.file.content_limit",
    "Only regular files can be saved": "error.file.regular_save",
    "The file changed after it was opened": "error.file.conflict",
    "Save destination escapes the workspace": "error.path.save_escape",
    "Invalid name": "error.file.invalid_name",
    "Invalid upload filename": "error.file.invalid_upload_name",
    "Upload target is not a regular file": "error.file.upload_target",
    "Symbolic links cannot be renamed": "error.file.symlink_rename",
    "The workspace root cannot be deleted": "error.file.root_delete",
    "Symbolic links cannot be deleted": "error.file.symlink_delete",
    "Absolute paths are not allowed": "error.path.absolute",
    "Path escapes the allowed boundary": "error.path.escape",
    "Termroom internal config is not exposed through Files": "error.path.internal_config",
    "Terminal disappeared while renaming": "error.terminal.disappeared",
    "Upload exceeds the configured size limit": "files.error.too_large_generic",
    "git is not installed on the remote computer": "remote_run.error.git_missing",
}

ERROR_CODE_KEYS = {
    "command_required": "remote_run.error.command_required",
    "git_clone_failed": "remote_run.failed.git_copy",
    "git_missing": "remote_run.error.git_missing",
    "git_url_control": "remote_run.error.public_https_git",
    "git_url_fragment": "remote_run.error.public_https_git",
    "git_url_host": "remote_run.error.public_https_git",
    "git_url_invalid": "remote_run.error.public_https_git",
    "git_url_path": "remote_run.error.public_https_git",
    "git_url_query": "remote_run.error.public_https_git",
    "git_url_required": "remote_run.error.public_https_git",
    "git_url_scheme": "remote_run.error.public_https_git",
    "git_url_userinfo": "remote_run.error.public_https_git",
    "git_url_whitespace": "remote_run.error.public_https_git",
    "source_contains_run_base": "remote_run.error.workspace_required",
    "source_path": "remote_run.error.workspace_required",
    "target_required": "remote_run.error.target_required",
    "workspace_required": "remote_run.error.workspace_required",
    "zip_extension": "remote_run.error.zip_only",
    "zip_filename": "remote_run.error.zip_required",
}

OS_ERROR_MESSAGE_KEYS = {
    errno.ENOENT: "error.os.not_found",
    errno.ENOTDIR: "error.os.not_directory",
    errno.EACCES: "error.os.permission",
    errno.EPERM: "error.os.permission",
    errno.ENOTEMPTY: "error.os.not_empty",
    errno.ENOSPC: "error.os.no_space",
    errno.EROFS: "error.os.read_only",
}


def normalize_locale(value: str | None) -> str:
    raw = (value or "").strip().lower().replace("_", "-")
    language = raw.split("-", 1)[0]
    return language if language in SUPPORTED_LOCALES else DEFAULT_LOCALE


def locale_from_request(request: Request) -> str:
    cookie = request.cookies.get(LOCALE_COOKIE)
    if cookie:
        return normalize_locale(cookie)
    settings = getattr(request.app.state, "settings", None)
    configured = getattr(settings, "default_locale", DEFAULT_LOCALE)
    return normalize_locale(str(configured))


@lru_cache(maxsize=8)
def messages(locale: str) -> dict[str, str]:
    normalized = normalize_locale(locale)
    path = LOCALES_DIR / f"{normalized}.json"
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Locale file must contain an object: {path}")
    return {str(key): str(value) for key, value in data.items()}


def translate(locale: str, key: str, **values: Any) -> str:
    primary = messages(locale)
    fallback = messages(DEFAULT_LOCALE)
    value = primary.get(key, fallback.get(key, key))
    if not values:
        return value
    try:
        return value.format(**values)
    except (KeyError, ValueError):
        return value


def localize_error_code(locale: str, code: Any, **values: Any) -> str | None:
    key = ERROR_CODE_KEYS.get(str(code or ""))
    return translate(locale, key, **values) if key else None


def localize_exception(locale: str, exc: BaseException) -> str:
    locale_key = getattr(exc, "locale_key", None)
    if locale_key:
        values = getattr(exc, "locale_values", {}) or {}
        return translate(locale, str(locale_key), **values)
    coded = localize_error_code(locale, getattr(exc, "code", None))
    if coded:
        return coded
    key = ERROR_MESSAGE_KEYS.get(str(exc))
    if key:
        return translate(locale, key)
    if isinstance(exc, OSError):
        key = OS_ERROR_MESSAGE_KEYS.get(exc.errno)
        if key:
            return translate(locale, key)
    return str(exc)


def template_context(request: Request) -> dict[str, Any]:
    locale = locale_from_request(request)

    def t(key: str, **values: Any) -> str:
        return translate(locale, key, **values)

    return {
        "locale": locale,
        "authenticated": bool(getattr(request.state, "session", None)),
        "languages": {
            code: messages(code).get("language.name", code)
            for code in SUPPORTED_LOCALES
        },
        "t": t,
        "i18n_messages": messages(locale),
    }
