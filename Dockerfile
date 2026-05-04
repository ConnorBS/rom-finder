FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc curl unzip \
    && rm -rf /var/lib/apt/lists/*

# RAHasher is distributed via RALibretro releases (not a standalone repo).
# Without it, disc-based systems (Saturn, PS1/2, Dreamcast, etc.) fall back to
# a plain MD5 of the image file, which will never match RA's disc hash.
RUN HASHER_URL=$(curl -fsSL \
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

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

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
