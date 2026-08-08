from __future__ import annotations

from pathlib import Path

from termroom.secrets import SecretStore


def test_secret_store_encrypts_and_survives_reopen(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "config")
    store.put("computer-1", "correct horse battery staple")

    credential = tmp_path / "config" / "credentials" / "computer-1"
    assert credential.is_file()
    assert b"correct horse battery staple" not in credential.read_bytes()
    assert credential.stat().st_mode & 0o077 == 0
    assert (tmp_path / "config" / "credential-key").stat().st_mode & 0o077 == 0

    reopened = SecretStore(tmp_path / "config")
    assert reopened.get("computer-1") == "correct horse battery staple"
