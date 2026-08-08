#!/bin/sh
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

case "$PUID" in
  ''|*[!0-9]*)
    echo "PUID and PGID must be positive numeric IDs." >&2
    exit 2
    ;;
esac
case "$PGID" in
  ''|*[!0-9]*)
    echo "PUID and PGID must be positive numeric IDs." >&2
    exit 2
    ;;
esac

if [ "$PUID" -eq 0 ] || [ "$PGID" -eq 0 ]; then
  echo "Termroom refuses PUID=0 or PGID=0. Use a dedicated non-root host user." >&2
  exit 2
fi

current_gid="$(id -g termroom)"
current_uid="$(id -u termroom)"
if [ "$current_gid" -ne "$PGID" ]; then
  groupmod -o -g "$PGID" termroom
fi
if [ "$current_uid" -ne "$PUID" ]; then
  usermod -o -u "$PUID" -g "$PGID" termroom
fi

mkdir -p /config /config/home /config/ssh /config/credentials /workspaces
chown -R "$PUID:$PGID" /config
chmod 700 /config /config/home /config/ssh /config/credentials

export HOME=/config/home
umask 077
exec gosu termroom termroom "$@"
