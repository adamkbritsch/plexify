"""Delete a playlist zip once it's been downloaded directly off the NAS.

inotify-based: when an outside process (SMB / filebrowser) reads the zip and then
closes it, we delete it. Filesystem events propagate across the bind mount even
though the reader runs on the host, so NO host-PID/root access is needed. The
15-minute expiry (for zips that are never grabbed) is handled separately by the
per-zip timer in routes.py.
"""
import os
import logging
import threading
import time

log = logging.getLogger(__name__)
_DIR = "/Volumes/MediaVolume3/Downloads/music/playlist-zips"


def _run():
    try:
        from inotify_simple import INotify, flags
    except Exception as e:
        log.warning("zip_watcher: inotify_simple unavailable (%s) — post-download cleanup off", e)
        return
    try:
        os.makedirs(_DIR, exist_ok=True)
        ino = INotify()
        ino.add_watch(_DIR, flags.ACCESS | flags.CLOSE_NOWRITE | flags.CREATE | flags.MOVED_TO)
    except Exception as e:
        log.warning("zip_watcher: setup failed (%s)", e)
        return
    read = set()
    log.info("zip_watcher: watching %s for completed downloads", _DIR)
    while True:
        try:
            for ev in ino.read(timeout=None):
                nm = ev.name or ""
                if not nm.endswith(".zip"):
                    continue
                fp = os.path.join(_DIR, nm)
                if ev.mask & flags.ACCESS:
                    read.add(nm)                      # it's actually being read (a download)
                elif ev.mask & flags.CLOSE_NOWRITE:
                    if nm in read:                    # read AND now closed -> download finished
                        try:
                            os.remove(fp)
                            log.info("zip_watcher: deleted after download — %s", nm)
                        except OSError:
                            pass
                        read.discard(nm)
                elif ev.mask & (flags.CREATE | flags.MOVED_TO):
                    read.discard(nm)                  # fresh build -> reset
        except Exception:
            log.exception("zip_watcher loop error")
            time.sleep(2)


def start():
    threading.Thread(target=_run, name="zip-watcher", daemon=True).start()
