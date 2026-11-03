#!/bin/bash
cd /opt/data/mediaexplorer
export DATA_PATH=/opt/data/cache/scratch/mxdata
export MEDIA_1='Photos|/opt/data/cache/scratch/testlib/photos'
export MEDIA_2='Work|/opt/data/cache/scratch/testlib/work'
export APP_NAME=MediaExplorer
export PORT=8765
export MX_PACK_MODE=adjacent
export MX_HWACCEL=none
exec /opt/data/cache/scratch/mxenv/bin/python -m uvicorn app.server:app --host 127.0.0.1 --port 8765
