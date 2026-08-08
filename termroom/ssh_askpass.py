from __future__ import annotations

import os
import sys
from pathlib import Path

from termroom.secrets import SecretStore, SecretStoreError


def main() -> int:
    config_dir = os.environ.get("TERMROOM_CONFIG_DIR", "")
    secret_id = os.environ.get("TERMROOM_SSH_CREDENTIAL_ID", "")
    if not config_dir or not secret_id:
        return 1
    try:
        value = SecretStore(Path(config_dir)).get(secret_id)
    except (OSError, ValueError, SecretStoreError):
        return 1
    sys.stdout.write(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
