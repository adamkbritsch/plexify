"""Manual music import — the user drops files into an import folder and Plexify keeps the good
FLAC music (sorting it into the Plex library even if it isn't a liked song), discards the junk,
and optionally deletes it. This is how the gaps the automated sources can't get (surfaced by the
suggestor) get closed by hand.

Reuses the download-sweep machinery from autofill_engine (integrity, tag reading, sanitize, dedup,
tag-stamp, recovery ledger). Runs Mac-side against the SMB-mounted import folder, exactly like the
orphan sweep runs against the download dirs — no NAS↔Mac path translation needed.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import shutil
import time

from .db import get_config

log = logging.getLogger(__name__)

_AUDIO_EXTS = (".flac", ".mp3", ".m4a", ".alac", ".aac", ".ogg", ".opus", ".wav", ".wma", ".aiff")
_NONMUSIC_GENRES = {"podcast", "podcasts", "audiobook", "audiobooks", "audio book", "spoken word",
                    "spoken", "speech", "sound effect", "sound effects", "sfx", "asmr"}
_MIN_DURATION_S = 30          # shorter than this = a clip/interlude, not a song
_INFLIGHT_SECS = 2 * 60       # skip files touched in the last 2 min (still copying)
DEFAULT_IMPORT_PATH = "/Volumes/MediaVolume3/Downloads/music/import"


def _cfg_bool(key: str, default: str = "0") -> bool:
    return (get_config(key, default) or default) == "1"


def manual_import_enabled() -> bool:
    return _cfg_bool("manual_import_enabled")


def _import_path() -> str:
    return get_config("manual_import_path", DEFAULT_IMPORT_PATH) or DEFAULT_IMPORT_PATH


def _flac_quality(path: str) -> tuple[int, int]:
    """(bits, sample_rate) for a FLAC file, or (0, 0) — used for the upgrade-if-better compare."""
    try:
        from mutagen.flac import FLAC
        info = FLAC(path).info
        return (int(getattr(info, "bits_per_sample", 0) or 0), int(getattr(info, "sample_rate", 0) or 0))
    except Exception:
        return (0, 0)


def _read_tags(path: str) -> tuple:
    """(artist, album, title, genre, duration_seconds), best-effort via mutagen."""
    try:
        from mutagen import File as MutagenFile
        m = MutagenFile(path, easy=True)
        if not m:
            return ("", "", "", "", 0.0)
        t = m.tags or {}
        return ((t.get("albumartist") or t.get("artist") or [""])[0],
                (t.get("album") or [""])[0],
                (t.get("title") or [""])[0],
                (t.get("genre") or [""])[0],
                float(getattr(m.info, "length", 0.0) or 0.0))
    except Exception:
        return ("", "", "", "", 0.0)


def _find_existing(dest_dir: str, title_key: str, artist: str):
    """Path of an existing file in dest_dir whose normalized title matches title_key, or None."""
    from .autofill_engine import _file_title_key
    if not title_key or not os.path.isdir(dest_dir):
        return None
    for fn in os.listdir(dest_dir):
        p = os.path.join(dest_dir, fn)
        if os.path.isfile(p) and _file_title_key(p, artist) == title_key:
            return p
    return None


def manual_import_scan(dry_run: bool = False) -> dict:
    """Classify every audio file in the import folder → keep (sort into the library, upgrading a
    worse existing copy) or unnecessary (delete if the toggle is on, else quarantine). Returns a
    tally; under dry_run nothing is moved/deleted."""
    from .autofill_engine import (_verify_flac_integrity, _file_title_key, _safe_for_fs,
                                   _stamp_file_tags, _album_from_db, _log_recovery_move,
                                   _known_artists, _intent_title_keys, _LOCAL_PATH_PREFIX)

    out = {"scanned": 0, "imported": 0, "upgraded": 0, "deleted": 0, "quarantined": 0,
           "skipped_inflight": 0, "by_reason": {}, "bytes": 0, "dry_run": bool(dry_run)}
    root = _import_path()
    if not os.path.isdir(root):
        out["error"] = "import folder not found: %s" % root
        return out

    MUSIC = _LOCAL_PATH_PREFIX
    require_liked = _cfg_bool("manual_import_require_liked")
    delete_mode = _cfg_bool("manual_import_delete_unnecessary")
    now = time.time()
    datestamp = _dt.datetime.utcnow().strftime("%Y-%m-%d")

    def _reason(r):
        out["by_reason"][r] = out["by_reason"].get(r, 0) + 1

    def _dispose(path, reason):
        """Discard an unnecessary file: delete (toggle on) or quarantine to _unnecessary/{date}/."""
        _reason(reason)
        if dry_run:
            return
        if delete_mode:
            try:
                os.remove(path)
                _log_recovery_move("import_delete:" + reason, path, "(deleted)")
                out["deleted"] += 1
            except OSError:
                log.exception("manual_import: delete failed for %s", path)
        else:
            qdir = os.path.join(root, "_unnecessary", datestamp)
            try:
                os.makedirs(qdir, exist_ok=True)
                dst = os.path.join(qdir, os.path.basename(path))
                shutil.move(path, dst)
                _log_recovery_move("import_quarantine:" + reason, path, dst)
                out["quarantined"] += 1
            except OSError:
                log.exception("manual_import: quarantine failed for %s", path)

    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d != "_unnecessary"]   # never descend into our quarantine
        for fn in fns:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in _AUDIO_EXTS:
                continue                                    # leave non-audio (art/cue/nfo) alone
            path = os.path.join(dp, fn)
            try:
                if now - os.path.getmtime(path) < _INFLIGHT_SECS:
                    out["skipped_inflight"] += 1
                    continue
            except OSError:
                continue
            out["scanned"] += 1

            # 1. lossy → discard (FLAC only)
            if ext != ".flac":
                _dispose(path, "lossy"); continue
            # 2. integrity (FLAC magic + ffmpeg decode)
            intact, bad = _verify_flac_integrity([path])
            if bad or not intact:
                _dispose(path, "corrupt"); continue
            # 3. non-music / tiny-clip gate (conservative — keep when unsure)
            artist, album, title, genre, dur = _read_tags(path)
            if dur and dur < _MIN_DURATION_S:
                _dispose(path, "tiny"); continue
            if genre and genre.strip().lower() in _NONMUSIC_GENRES:
                _dispose(path, "non_music"); continue
            # 4. attribution: tags → "Title - Artist" filename rescue (known artists only) → db album
            if not artist:
                base = os.path.splitext(fn)[0]
                if " - " in base:
                    _l, _, _r = base.rpartition(" - ")
                    ka = _known_artists()
                    if _r.strip().lower() in ka:
                        artist = _r.strip()
                    elif _l.strip().lower() in ka:
                        artist = _l.strip()
            if artist and not album:
                album = _album_from_db(title, artist) or album
            if not artist or not album:
                _dispose(path, "unattributable"); continue
            # 5. optional strict mode — only music matching a liked/playlist song
            tkey = _file_title_key(path, artist)
            if require_liked and tkey and tkey not in _intent_title_keys():
                _dispose(path, "not_wanted"); continue
            # 6. dedup + upgrade-if-better
            dest = os.path.join(MUSIC, _safe_for_fs(artist), _safe_for_fs(album, "Unknown Album"))
            existing = _find_existing(dest, tkey, artist)
            is_upgrade = False
            if existing:
                if _flac_quality(path) > _flac_quality(existing):
                    is_upgrade = True                       # replace + attic the worse copy
                else:
                    _dispose(path, "duplicate"); continue
            # 7. place the keeper
            if dry_run:
                _reason("upgrade" if is_upgrade else "import")
                out["upgraded" if is_upgrade else "imported"] += 1
                try:
                    out["bytes"] += os.path.getsize(path)
                except OSError:
                    pass
                continue
            try:
                os.makedirs(dest, exist_ok=True)
                if is_upgrade and existing:
                    adir = os.path.join(root, "_unnecessary", datestamp, "_replaced")
                    os.makedirs(adir, exist_ok=True)
                    try:
                        adst = os.path.join(adir, os.path.basename(existing))
                        shutil.move(existing, adst)
                        _log_recovery_move("import_upgrade_old", existing, adst)
                    except OSError:
                        log.exception("manual_import: atticking replaced file failed")
                sz = os.path.getsize(path)
                dst = os.path.join(dest, fn)
                shutil.move(path, dst)
                try:
                    _stamp_file_tags(dst, artist, album)
                except Exception:
                    pass
                _log_recovery_move("import_upgrade" if is_upgrade else "import", path, dst)
                out["upgraded" if is_upgrade else "imported"] += 1
                out["bytes"] += sz
                _reason("upgrade" if is_upgrade else "import")
            except Exception:
                log.exception("manual_import: place failed for %s", path)
                _reason("place_error")

    # Trigger a Plex library scan if we placed anything (same pattern the album rules use).
    if not dry_run and (out["imported"] or out["upgraded"]):
        try:
            from . import plex_client
            srv = plex_client._connect()
            sec = plex_client._music_section(srv) if srv else None
            if sec:
                sec.update()
        except Exception:
            pass
    log.info("manual_import_scan(dry_run=%s): %s", dry_run, out)
    return out


def manual_import_scan_tick() -> dict:
    """Scheduler entry point — no-op unless the feature is enabled; honors the dry-run flag."""
    if not manual_import_enabled():
        return {"skipped": "manual_import disabled"}
    return manual_import_scan(dry_run=_cfg_bool("manual_import_dry_run"))
