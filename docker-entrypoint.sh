#!/bin/bash
set -e

# Unraid PUID/PGID convention
if [ -n "$PUID" ] && [ "$PUID" != "0" ] && [ "$(id -u)" = "0" ]; then
  PUGID=$(grep -E "mediaexplorer:.*:" /etc/group | cut -d: -f3 | head -1)
  if [ -z "$PUGID" ]; then
    groupadd -g "${PGID:-100}" mediaexplorer 2>/dev/null || true
    useradd -u "${PUID}" -g "${PGID:-100}" -M -s /nologin mediaexplorer 2>/dev/null || true
  fi
  exec gosu "${PUID}:${PGID:-100}" /app/docker-entrypoint.sh "$@"
fi

if [ "$1" = "serve" ]; then
  shift
  exec python -m uvicorn app.server:app --host 0.0.0.0 --port "${PORT:-8000}" "$@"
else
  exec "$@"
fi
