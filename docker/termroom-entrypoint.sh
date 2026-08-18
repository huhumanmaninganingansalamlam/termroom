#!/bin/sh
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
TERMROOM_MODE="${TERMROOM_MODE:-core}"
TERMROOM_SECURE_COOKIE="${TERMROOM_SECURE_COOKIE:-false}"

case "$TERMROOM_MODE" in
  core|node)
    ;;
  *)
    echo "TERMROOM_MODE must be either core or node." >&2
    exit 2
    ;;
esac

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

if [ "$TERMROOM_MODE" = "node" ] && [ "${1:-}" != "node" ]; then
  node_config_dir="${TERMROOM_NODE_CONFIG_DIR:-/config/node}"
  node_config_file="${node_config_dir}/node.json"
  if [ ! -f "$node_config_file" ]; then
    echo "Termroom Node is not paired. Waiting for pairing configuration in ${node_config_file}."
    echo "Pair this running container with:"
    echo "  docker exec -it -u termroom <container> termroom node --config-dir ${node_config_dir} pair ..."
    stop_pairing_wait=0
    trap 'stop_pairing_wait=1' TERM INT
    while [ ! -f "$node_config_file" ] && [ "$stop_pairing_wait" -eq 0 ]; do
      sleep 2 &
      wait "$!" || true
    done
    trap - TERM INT
    if [ "$stop_pairing_wait" -ne 0 ]; then
      exit 0
    fi
    echo "Pairing configuration found. Starting Termroom Node."
  fi
  set -- node --config-dir "$node_config_dir"
fi

if [ "$TERMROOM_MODE" = "core" ]; then
  case "$TERMROOM_SECURE_COOKIE" in
    1|true|TRUE|yes|YES|on|ON)
      set -- "$@" --secure-cookie
      ;;
    0|false|FALSE|no|NO|off|OFF|'')
      ;;
    *)
      echo "TERMROOM_SECURE_COOKIE must be true or false." >&2
      exit 2
      ;;
  esac
fi

exec gosu termroom termroom "$@"
