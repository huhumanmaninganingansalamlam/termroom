from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path


def default_config_dir() -> Path:
    explicit = os.environ.get("TERMROOM_CONFIG_DIR")
    if explicit:
        return Path(explicit).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    return (
        Path(base).expanduser() / "termroom"
        if base
        else Path.home() / ".config" / "termroom"
    )


def default_state_dir() -> Path:
    """Persistent Termroom config directory."""
    return default_config_dir()


@dataclass(frozen=True, slots=True)
class Settings:
    root: Path
    host: str = "127.0.0.1"
    port: int = 8765
    state_dir: Path = default_state_dir()
    access_token: str = ""
    login_password: str = ""
    default_locale: str = "en"
    allow_local_workspaces: bool = True
    secure_cookie: bool = False
    max_edit_bytes: int = 1024 * 1024
    max_preview_bytes: int = 512 * 1024
    max_upload_bytes: int = 2 * 1024 * 1024 * 1024

    @classmethod
    def create(
        cls,
        root: str | Path,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        state_dir: str | Path | None = None,
        access_token: str | None = None,
        login_password: str | None = None,
        default_locale: str | None = None,
        allow_local_workspaces: bool | None = None,
        secure_cookie: bool = False,
    ) -> Settings:
        root_path = Path(root).expanduser().resolve(strict=True)
        if not root_path.is_dir():
            raise ValueError(f"Root is not a directory: {root_path}")

        state_path = Path(state_dir).expanduser() if state_dir else default_state_dir()
        state_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            state_path.chmod(0o700)
        token = access_token or _load_or_create_token(state_path / "access-token")
        environment_password = os.environ.get("TERMROOM_PASSWORD") or ""
        config_dotenv_path = state_path / ".env"
        project_dotenv_path = root_path / ".env"
        config_dotenv_password = ""
        project_dotenv_password = ""
        if not login_password and not environment_password:
            config_dotenv_password = _load_dotenv_value(
                config_dotenv_path, "TERMROOM_PASSWORD"
            )
            if config_dotenv_password:
                _validate_password_file_permissions(config_dotenv_path)
        if not login_password and not environment_password and not config_dotenv_password:
            project_dotenv_password = _load_dotenv_value(
                project_dotenv_path, "TERMROOM_PASSWORD"
            )
            if project_dotenv_password:
                _validate_password_file_permissions(project_dotenv_path)
        password = (
            login_password
            or environment_password
            or config_dotenv_password
            or project_dotenv_password
            or (access_token if access_token else "")
        )
        minimum_password_length = _minimum_password_length()
        if password and minimum_password_length and len(password) < minimum_password_length:
            raise ValueError(
                "TERMROOM_PASSWORD must be at least "
                f"{minimum_password_length} characters because "
                "TERMROOM_MIN_PASSWORD_LENGTH is configured"
            )
        locale_value = (
            default_locale
            or os.environ.get("TERMROOM_LOCALE")
            or _load_dotenv_value(config_dotenv_path, "TERMROOM_LOCALE")
            or _load_dotenv_value(project_dotenv_path, "TERMROOM_LOCALE")
            or "en"
        )
        locale = _normalize_locale_setting(locale_value)
        if allow_local_workspaces is None:
            allow_local_workspaces_value = (
                os.environ.get("TERMROOM_ALLOW_LOCAL_WORKSPACES")
                or _load_dotenv_value(
                    config_dotenv_path, "TERMROOM_ALLOW_LOCAL_WORKSPACES"
                )
                or _load_dotenv_value(
                    project_dotenv_path, "TERMROOM_ALLOW_LOCAL_WORKSPACES"
                )
                or "true"
            )
            allow_local_workspaces = _normalize_boolean_setting(
                allow_local_workspaces_value,
                name="TERMROOM_ALLOW_LOCAL_WORKSPACES",
            )
        return cls(
            root=root_path,
            host=host,
            port=port,
            state_dir=state_path.resolve(),
            access_token=token,
            login_password=password,
            default_locale=locale,
            allow_local_workspaces=allow_local_workspaces,
            secure_cookie=secure_cookie,
        )

    @property
    def database_path(self) -> Path:
        return self.state_dir / "termroom.sqlite3"

    @property
    def csrf_token(self) -> str:
        payload = f"termroom-csrf:{self.access_token}".encode()
        return hashlib.sha256(payload).hexdigest()

def _load_or_create_token(path: Path) -> str:
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            if os.name != "nt":
                path.chmod(0o600)
            return token

    token = secrets.token_urlsafe(32)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(token + "\n", encoding="utf-8")
    if os.name != "nt":
        temporary.chmod(0o600)
    os.replace(temporary, path)
    return token


def _minimum_password_length() -> int:
    """Return the operator-selected password policy.

    Termroom is a single-user self-hosted tool. Password strength is therefore
    an operator policy rather than an application-level opinion. By default we
    accept any non-empty password; deployments that want a minimum can opt in
    with TERMROOM_MIN_PASSWORD_LENGTH.
    """

    raw = os.environ.get("TERMROOM_MIN_PASSWORD_LENGTH", "").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("TERMROOM_MIN_PASSWORD_LENGTH must be a non-negative integer") from exc
    if value < 0:
        raise ValueError("TERMROOM_MIN_PASSWORD_LENGTH must be a non-negative integer")
    return value


def _load_dotenv_value(path: Path, requested_name: str) -> str:
    if not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() != requested_name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value.strip()
    return ""


def _normalize_locale_setting(value: str) -> str:
    locale = value.strip().lower().replace("_", "-").split("-", 1)[0]
    if locale not in {"en", "ko"}:
        raise ValueError("TERMROOM_LOCALE must be either 'en' or 'ko'")
    return locale


def _normalize_boolean_setting(value: str, *, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _validate_password_file_permissions(path: Path) -> None:
    if os.name == "nt":
        return
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise ValueError(
            f"Termroom password file permissions are too open ({mode:o}). "
            f"Run: chmod 600 {path}"
        )
