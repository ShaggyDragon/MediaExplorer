#!/bin/bash
# MediaExplorer container launcher — one container per library.
#
#   NAME=photos PORT=8086 DATA=/mnt/user/appdata/mediaexplorer \
#   MEDIA1=/mnt/user/Photos MEDIA1_NAME=Photos ./run-container.sh
#
# Everything below is an env-overridable default. For a second library on the same
# box change exactly four things: NAME, PORT, DATA, MEDIA1.
#
# Two ways to run the image:
#   * IMAGE=mediaexplorer:1.0.0        (built locally: docker build -t mediaexplorer:1.0.0 .)
#   * SRC=<source dir> + image mount   (dev loop: the container executes live source,
#     so a code edit needs only POST /api/admin/restart — no rebuild)

set -u
NAME=${NAME:-mediaexplorer}
IMAGE=${IMAGE:-mediaexplorer:latest}
PORT=${PORT:-8086}
DATA=${DATA:-/mnt/user/appdata/mediaexplorer}   # index DB; keep it on SSD/cache
SRC=${SRC:-}                                    # set to mount source at /app (dev loop)
MEDIA1=${MEDIA1:-/mnt/user/Photos}
MEDIA1_NAME=${MEDIA1_NAME:-$(basename "${MEDIA1%/}")}
APP_NAME=${APP_NAME:-$MEDIA1_NAME}
TZ=${TZ:-UTC}
PUID_N=${PUID_N:-99}
PGID_N=${PGID_N:-100}
WIPE=${WIPE:-0}          # 1 = a VERSION change wipes every thumbnail store (dev builds);
                         # 0 = the cache is permanent and code deploys keep the thumbnails
PACK_MODE=${PACK_MODE:-adjacent}
                         # adjacent = a hidden .<name>.mxthumbs file BESIDE the media (default);
                         #            it travels with the media and with its backup
                         # central  = packs live in $DATA/packs; nothing is written into the
                         #            media tree — use it for a test instance pointed at a
                         #            real library, or a read-only media mount
USE_GPU=${USE_GPU:-0}    # 1 = pass through Nvidia for NVDEC/scale (needs the container toolkit)
MX_TOKEN=${MX_RESTART_TOKEN:-}   # enables /api/admin/* (restart, settings); optional

# optional extra shares: MEDIA2=/mnt/user/Videos MEDIA2_NAME=Videos ./run-container.sh
MEDIA2=${MEDIA2:-}; MEDIA2_NAME=${MEDIA2_NAME:-Share2}
MEDIA3=${MEDIA3:-}; MEDIA3_NAME=${MEDIA3_NAME:-Share3}

# Docker creates a missing bind source as root:root, and the app runs as PUID:PGID,
# so sqlite then fails with "unable to open database file" and the container
# crash-loops. Create and own the paths first (top level only — recursing a media
# share would be slow and wrong).
for d in "$DATA" "$MEDIA1" ${MEDIA2:-} ${MEDIA3:-}; do
  [ -n "$d" ] || continue
  [ -d "$d" ] || mkdir -p "$d" 2>/dev/null
  chown "$PUID_N:$PGID_N" "$d" 2>/dev/null || true
done

# Media mounts are :rw even for a library you only read: in adjacent mode the pack is
# written beside the media. A read-only mount makes the app fall back to central.
MOUNTS=(-v "$DATA":/data:rw -v "$MEDIA1":/media1:rw)
[ -n "${SRC:-}" ] && MOUNTS+=(-v "$SRC":/app:rw)
[ -n "$MEDIA2" ] && MOUNTS+=(-v "$MEDIA2":/media2:rw -e MEDIA_2_NAME="$MEDIA2_NAME")
[ -n "$MEDIA3" ] && MOUNTS+=(-v "$MEDIA3":/media3:rw -e MEDIA_3_NAME="$MEDIA3_NAME")

ENVS=(-e PUID="$PUID_N" -e PGID="$PGID_N" -e TZ="$TZ" -e APP_NAME="$APP_NAME"
      -e MEDIA_1_NAME="$MEDIA1_NAME" -e MX_PACK_MODE="$PACK_MODE"
      -e MX_WIPE_ON_BUILD="$WIPE" -e MX_THUMB_WORKERS=0 -e MX_VIDEO_JOBS=0)
[ -n "$MX_TOKEN" ] && ENVS+=(-e MX_RESTART_TOKEN="$MX_TOKEN")
[ "$USE_GPU" = "1" ] && ENVS+=(-e NVIDIA_DRIVER_CAPABILITIES=all -e MX_HWACCEL=cuda)

docker rm -f "$NAME" 2>/dev/null || true     # DB and packs persist on the host

GPU_ARGS=()
[ "$USE_GPU" = "1" ] && GPU_ARGS=(--gpus all)

docker run -d --name "$NAME" \
  --net=bridge \
  --restart unless-stopped \
  --pids-limit 2048 \
  "${GPU_ARGS[@]}" \
  -p "$PORT:8000/tcp" \
  "${ENVS[@]}" \
  "${MOUNTS[@]}" \
  "$IMAGE"

echo "started $NAME on :$PORT — waiting for the API"
for _ in $(seq 1 30); do
  V=$(curl -s -m 3 "http://127.0.0.1:$PORT/api/config" | grep -o '"version":"[^"]*"' | cut -d'"' -f4)
  if [ -n "$V" ]; then
    echo "up: version $V"
    [ -n "${SRC:-}" ] && echo "    repo VERSION: $(cat "$SRC/VERSION" 2>/dev/null)"
    exit 0
  fi
  sleep 2
done
echo "no answer in 60s: docker logs --tail 50 $NAME" >&2
exit 1
