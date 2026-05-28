FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc curl unzip \
    && rm -rf /var/lib/apt/lists/*

# RAHasher is distributed via RALibretro releases (not a standalone repo).
# Without it, disc-based systems (Saturn, PS1/2, Dreamcast, Wii/GameCube, etc.) fall
# back to a plain MD5 of the image file, which will never match RA's disc hash.
# This layer sits before the code COPY, so it's cached across code-only deploys.
# Bump RAHASHER_REFRESH to force a re-fetch of the latest binary.
ARG RAHASHER_REFRESH=2026-05-28
RUN echo "RAHasher fetch (refresh ${RAHASHER_REFRESH})" \
    && HASHER_URL=$(curl -fsSL \
        "https://api.github.com/repos/RetroAchievements/RALibretro/releases/latest" \
      | grep -o '"browser_download_url": "[^"]*RAHasher-x64-Linux[^"]*"' \
      | head -1 | sed 's/.*"browser_download_url": "//;s/"$//') \
    && curl -fsSL "$HASHER_URL" -o /tmp/RAHasher.zip \
    && mkdir -p /tmp/rahasher \
    && unzip -j /tmp/RAHasher.zip -d /tmp/rahasher/ \
    && find /tmp/rahasher -maxdepth 1 -name 'RAHasher*' -type f \
       | head -1 | xargs -I{} install -m 755 {} /usr/local/bin/RAHasher \
    && rm -rf /tmp/RAHasher.zip /tmp/rahasher \
    || echo "RAHasher download failed — Python fallback hashing will be used"

# nodtool (nod-rs): RAHasher cannot read compressed GameCube/Wii disc images
# (RVZ/WBFS/WIA/GCZ/CISO — RALibretro issue #415 unimplemented). nodtool decompresses
# them to a raw ISO so RAHasher can then produce the correct RA disc hash.
ARG NODTOOL_VERSION=v2.0.0-alpha.9
RUN curl -fsSL \
      "https://github.com/encounter/nod/releases/download/${NODTOOL_VERSION}/nodtool-linux-x86_64" \
      -o /usr/local/bin/nodtool \
    && chmod 755 /usr/local/bin/nodtool \
    || echo "nodtool download failed — compressed Wii/GameCube dumps won't be hashable"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium + its system libs for the Vimm extension (Playwright bypasses Vimm's
# JS download challenge). Chromium only — keeps the image ~400MB smaller than
# installing all browsers. Layer ordered after requirements so it caches.
RUN playwright install --with-deps chromium

COPY . .

RUN mkdir -p /app/static/covers /data

VOLUME ["/data", "/app/static/covers", "/roms"]

EXPOSE 8080

ARG APP_VERSION=dev
ENV HOST=0.0.0.0 \
    PORT=8080 \
    DEBUG=false \
    DB_URL=sqlite:////data/rom_finder.db \
    APP_VERSION=$APP_VERSION

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
