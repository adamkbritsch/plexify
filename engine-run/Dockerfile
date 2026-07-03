FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    tini \
    ffmpeg \
    build-essential \
    libffi-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

# SpotiFLAC XDG cache dir -- must exist and be writable by the non-root UID.
# (SpotiFLAC's git-main import bug is patched at runtime on startup — main.py _patch_sf().)
RUN mkdir -p /tmp/spotiflac-cache && chmod 777 /tmp/spotiflac-cache

COPY app/ ./app/
COPY downloader_daemon.py ./

# Data dir + music/downloads mount points. The engine's historical hardcoded paths
# (/Volumes/MediaVolume3/…) and the daemon's (/downloads_music) are symlinked onto the
# clean container mounts /music + /downloads so docker-compose can use tidy volume paths.
RUN mkdir -p /data /music /downloads /downloads/import /Volumes/MediaVolume3/Downloads \
    && ln -sfn /music /Volumes/MediaVolume3/plexify-music \
    && ln -sfn /downloads /Volumes/MediaVolume3/Downloads/music \
    && ln -sfn /downloads /downloads_music \
    && chown -R 1000:10 /data /music /downloads /Volumes

# SpotiFLAC uses Path.home() which resolves to HOME; set HOME and XDG_CACHE_HOME
# to /tmp paths so non-root uid 1000 can write provider token caches
ENV HOME=/tmp \
    XDG_CACHE_HOME=/tmp/spotiflac-cache

USER 1000:10

EXPOSE 8787


HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz').read()" || exit 1
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["gunicorn", "--bind", "0.0.0.0:8787", "--workers", "1", "--threads", "4", "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-", "app.main:app"]
