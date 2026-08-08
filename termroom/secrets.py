from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SecretStoreError(RuntimeError):
    pass


class SecretStore:
    """Small owner-only encrypted credential store under Termroom config.

    The encryption key and encrypted credential blobs live in the same
    persistent config volume but never in the project workspace. This is not a
    hardware-backed vault; it prevents accidental plaintext exposure in the
    SQLite DB, environment, logs, backups of project files, and browser UI.
    """

    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir
        self.key_path = config_dir / "credential-key"
        self.credentials_dir = config_dir / "credentials"

    def initialize(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.credentials_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.config_dir.chmod(0o700)
        self.credentials_dir.chmod(0o700)
        if not self.key_path.exists():
            temporary = self.key_path.with_suffix(".tmp")
            temporary.write_bytes(Fernet.generate_key() + b"\n")
            temporary.chmod(0o600)
            os.replace(temporary, self.key_path)
        self.key_path.chmod(0o600)

    def put(self, secret_id: str, value: str) -> None:
        if not secret_id or not value:
            raise ValueError("Secret id and value are required")
        self.initialize()
        encrypted = self._fernet().encrypt(value.encode("utf-8"))
        target = self._path(secret_id)
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(encrypted + b"\n")
        temporary.chmod(0o600)
        os.replace(temporary, target)
        target.chmod(0o600)

    def get(self, secret_id: str) -> str:
        self.initialize()
        target = self._path(secret_id)
        try:
            encrypted = target.read_bytes().strip()
        except OSError as exc:
            raise SecretStoreError("Stored SSH credential is unavailable") from exc
        try:
            return self._fernet().decrypt(encrypted).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise SecretStoreError("Stored SSH credential cannot be decrypted") from exc

    def delete(self, secret_id: str) -> None:
        try:
            self._path(secret_id).unlink()
        except FileNotFoundError:
            return

    def _fernet(self) -> Fernet:
        try:
            key = self.key_path.read_bytes().strip()
        except OSError as exc:
            raise SecretStoreError("Termroom credential key is unavailable") from exc
        try:
            return Fernet(key)
        except (ValueError, TypeError) as exc:
            raise SecretStoreError("Termroom credential key is invalid") from exc

    def _path(self, secret_id: str) -> Path:
        safe = "".join(
            character
            for character in secret_id
            if character.isalnum() or character in "-_"
        )
        if safe != secret_id or not safe:
            raise ValueError("Invalid secret id")
        return self.credentials_dir / safe
