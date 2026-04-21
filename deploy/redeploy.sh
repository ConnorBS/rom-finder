#!/bin/sh
# Pulls the latest rom-finder image and restarts the container on Unraid.
# Exact volume/env config is mirrored from the live Unraid Docker template.
set -e

IMAGE="ghcr.io/connorbs/rom-finder:latest"

echo "[$(date)] Pulling $IMAGE ..."
docker pull "$IMAGE"

echo "[$(date)] Stopping old container ..."
docker stop rom-finder 2>/dev/null || true
docker rm   rom-finder 2>/dev/null || true

echo "[$(date)] Starting new container ..."
docker run -d \
  --name rom-finder \
  --restart no \
  -p 19846:8080 \
  -e TZ=America/New_York \
  -e HOST_OS=Unraid \
  -e HOST_HOSTNAME=HomeBase \
  -e HOST_CONTAINERNAME=rom-finder \
  -e HOST=0.0.0.0 \
  -e PORT=8080 \
  -e DEBUG=false \
  -e DB_URL=sqlite:////data/rom_finder.db \
  -v /mnt/ssd_cache/appdata/rom-finder/data:/data:rw \
  -v /mnt/ssd_cache/appdata/rom-finder/covers:/app/static/covers:rw \
  -v "/mnt/user/Cloud Storage/Tools/Emulators/Roms/:/roms:rw" \
  -v /mnt/ssd_cache/Media/ROMs:/rom-check:rw \
  --log-opt max-size=50m \
  --log-opt max-file=2 \
  --label "net.unraid.docker.icon=https://github.com/ConnorBS/rom-finder/blob/main/rom-finder-extension/icons/icon128.png?raw=true" \
  --label "net.unraid.docker.managed=dockerman" \
  --label "net.unraid.docker.webui=http://[IP]:[PORT:19846]/" \
  "$IMAGE"

echo "[$(date)] Redeploy complete."
