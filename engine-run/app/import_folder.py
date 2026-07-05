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
import json
import logging
import os
import shutil
import time

from .db import get_config

log = logging.getLogger(__name__)

_AUDIO_EXTS = (".flac", ".mp3", ".m4a", ".alac", ".aac", ".ogg", ".opus", ".wav", ".wma", ".aiff")
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff")   # covers
_NONMUSIC_GENRES = {"podcast", "podcasts", "audiobook", "audiobooks", "audio book", "spoken word",
                    "spoken", "speech", "sound effect", "sound effects", "sfx", "asmr"}
_DEFAULT_MIN_SECONDS = 15     # below this a FLAC is treated as a clip, not a song. Kept LOW on
                              # purpose — real songs get short (the Beatles' "Her Majesty" is 23s),
                              # so only true micro-clips are caught. Tune via manual_import_min_seconds.
_INFLIGHT_SECS = 2 * 60       # skip files touched in the last 2 min (still copying)
DEFAULT_IMPORT_PATH = "/Volumes/MediaVolume3/Downloads/music/import"


def _cfg_bool(key: str, default: str = "0") -> bool:
    return (get_config(key, default) or default) == "1"


def manual_import_enabled() -> bool:
    return _cfg_bool("manual_import_enabled")


_PENDING_CACHE = [0.0, 0]   # (ts, count) — os.walk over SMB is slow, so cache it


def pending_count() -> int:
    """Settled (fully-uploaded, past the in-flight window) audio files in the import folder that
    are waiting to be organized — the 'staging' count for manual drops. Cached ~12s."""
    now = time.time()
    if now - _PENDING_CACHE[0] < 12:
        return _PENDING_CACHE[1]
    n = 0
    root = _import_path()
    if manual_import_enabled() and os.path.isdir(root):
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if d != "_unnecessary"]
            for fn in fns:
                if os.path.splitext(fn)[1].lower() in _AUDIO_EXTS:
                    try:
                        if now - os.path.getmtime(os.path.join(dp, fn)) >= _INFLIGHT_SECS:
                            n += 1
                    except OSError:
                        pass
    _PENDING_CACHE[0], _PENDING_CACHE[1] = now, n
    return n


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


def _record_imported_album(artist: str, album: str, new_paths: list) -> None:
    """Upsert ONE album's 'imported' AutofillAction row in its OWN short session, committing
    immediately — so each placed song becomes visible to the Recently-Added feed MID-SCAN
    (the feed windows on desc(last_attempt_at) and lists every imported_paths entry as a song).
    Idempotent: merges new_paths into imported_paths via set-union, so calling it per file
    correctly accumulates an album's tracks across dirs. WAL + busy_timeout make these frequent
    small commits safe against the concurrent feed reader. Never raises — a DB hiccup must not
    abort the file-placement scan."""
    try:
        from .db import session_scope as _ss, AutofillAction
        from .autofill_engine import _normalize_for_key
        from sqlalchemy import select as _sel
        ak = _normalize_for_key(artist or "")
        bk = _normalize_for_key(album or "")
        with _ss() as s:                                    # session_scope auto-commits on exit
            row = s.scalar(_sel(AutofillAction)
                           .where(AutofillAction.artist_key == ak)
                           .where(AutofillAction.album_key == bk))
            if row and row.status == "complete_locked":
                return                                      # final stage — immutable
            if not row:
                row = AutofillAction(artist=artist, album=album, artist_key=ak, album_key=bk,
                                     status="imported", pre_existing_files=0,
                                     source="manual-import", source_detail="Manual import")
                s.add(row)
                s.flush()
            else:
                row.status = "imported"
                row.source = row.source or "manual-import"
                row.source_detail = row.source_detail or "Manual import"
            row.last_attempt_at = _dt.datetime.utcnow()     # keep the row inside the feed's top-250 window
            try:
                _cur = json.loads(row.imported_paths or "[]")
            except Exception:
                _cur = []
            _all = sorted(set(_cur) | set(new_paths))
            row.imported_paths = json.dumps(_all)
            row.total_size_bytes = sum(os.path.getsize(p) for p in _all if os.path.exists(p))
            row.note = ("manual import: %d files" % len(_all))[:1024]
    except Exception:
        log.exception("manual_import: incremental record failed for %s / %s", artist, album)


def _resolve_unmatched_for_imports(pairs) -> int:
    """A placed song makes its wanted track no longer 'unmatched' — so DELETE the matching
    UnmatchedTrack (target=plex) row(s) and unstick the negative cache. `pairs` = iterable of
    (artist, title) read from the placed files' TITLE tags. Both sides are normalized through
    _core_ta (strips '- Remastered 2015 / Mono / Live', parens, feat.) for the title and _norm_ta
    for the artist, so 'Hey Jude - Remastered 2015' in Unmatched resolves against an imported
    'Hey Jude'. STRICT equality on BOTH artist and title-core — a Beatles import can never clear
    another artist's same-title row. Deletes ALL matching rows (the table has duplicates). Also
    clears TrackMapping.plex_searched_at (only where still unmatched) so the next Plex match can
    promote the track to 'covered' and drop it from the suggestor gap. Never raises."""
    try:
        from .db import session_scope as _ss, UnmatchedTrack, TrackMapping
        from .autofill_engine import _norm_ta, _core_ta
        from sqlalchemy import select as _sel, update as _upd
        wanted = set()
        for a, t in pairs:
            ak = _norm_ta(a or "")
            tk = _core_ta(t or "")
            if ak and tk:
                wanted.add((ak, tk))
        if not wanted:
            return 0
        deleted = 0
        track_ids: set = set()
        with _ss() as s:                                    # session_scope auto-commits on exit
            rows = list(s.scalars(
                _sel(UnmatchedTrack).where(UnmatchedTrack.target_service == "plex")
            ).all())
            for r in rows:
                if (_norm_ta(r.artist or ""), _core_ta(r.title or "")) in wanted:
                    if r.source_track_id:
                        track_ids.add(r.source_track_id)
                    s.delete(r)
                    deleted += 1
            if track_ids:
                s.execute(_upd(TrackMapping)
                          .where(TrackMapping.spotify_track_id.in_(track_ids),
                                 TrackMapping.plex_track_key.is_(None))
                          .values(plex_searched_at=None))
        return deleted
    except Exception:
        log.exception("manual_import: unmatched resolve failed")
        return 0


def manual_import_scan(dry_run: bool = False) -> dict:
    """Classify every audio file in the import folder → keep (sort into the library, upgrading a
    worse existing copy) or unnecessary (delete if the toggle is on, else quarantine). Returns a
    tally; under dry_run nothing is moved/deleted."""
    from .autofill_engine import (_verify_flac_integrity, _file_title_key, _safe_for_fs,
                                   _stamp_file_tags, _album_from_db, _log_recovery_move,
                                   _known_artists, _intent_title_keys, _normalize_for_key,
                                   _LOCAL_PATH_PREFIX)

    out = {"scanned": 0, "imported": 0, "upgraded": 0, "deleted": 0, "quarantined": 0,
           "covers": 0, "skipped_inflight": 0, "by_reason": {}, "bytes": 0, "dry_run": bool(dry_run)}
    root = _import_path()
    if not os.path.isdir(root):
        out["error"] = "import folder not found: %s" % root
        return out

    MUSIC = _LOCAL_PATH_PREFIX
    require_liked = _cfg_bool("manual_import_require_liked")
    delete_mode = _cfg_bool("manual_import_delete_unnecessary")
    songs_only = _cfg_bool("manual_import_songs_only")   # keep only songs (+ covers); delete other junk
    try:
        min_sec = float(get_config("manual_import_min_seconds", str(_DEFAULT_MIN_SECONDS)) or _DEFAULT_MIN_SECONDS)
    except (TypeError, ValueError):
        min_sec = _DEFAULT_MIN_SECONDS
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

    dir_dest: dict = {}                                    # source dir -> {dest album path: n songs}
    moved_by_album: dict = {}                              # (artist, album) -> [placed file paths]
    placed_titles: set = set()                             # (artist, title) of every placed song → clears Unmatched
    # ── PASS 1: songs → library; note which album each source dir's songs landed in ──
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d != "_unnecessary"]   # never descend into our quarantine
        audio = [f for f in fns if os.path.splitext(f)[1].lower() in _AUDIO_EXTS]

        for fn in audio:
            path = os.path.join(dp, fn)
            ext = os.path.splitext(fn)[1].lower()
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
            if dur and min_sec > 0 and dur < min_sec:
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
            # 7. place the keeper (this dir's covers ride into the album it lands in)
            dir_dest.setdefault(dp, {})[dest] = dir_dest.setdefault(dp, {}).get(dest, 0) + 1
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
                moved_by_album.setdefault((artist, album), []).append(dst)
                out["upgraded" if is_upgrade else "imported"] += 1
                out["bytes"] += sz
                _reason("upgrade" if is_upgrade else "import")
                # Commit this album's row NOW (idempotent merge) so the song live-appears in
                # Recently Added while the scan is still running — instead of only at the end.
                _record_imported_album(artist, album, [dst])
                placed_titles.add((artist, title))          # this song is no longer 'unmatched'
            except Exception:
                log.exception("manual_import: place failed for %s", path)
                _reason("place_error")

    # ── Cover→album resolver. Propagate each dir's placed album up to its ancestors, so covers in
    # a sibling "Artwork/" subfolder attach to the album the songs landed in; fall back to matching
    # the source folder name against an EXISTING library album (covers for already-imported albums).
    if songs_only:
        agg: dict = {}
        for _d, _counts in dir_dest.items():
            _a = _d
            while _a != root and _a.startswith(root):
                _ag = agg.setdefault(_a, {})
                for _k, _v in _counts.items():
                    _ag[_k] = _ag.get(_k, 0) + _v
                _p = os.path.dirname(_a)
                if _p == _a:
                    break
                _a = _p

        def _cover_dest(cdir):
            # A cover attaches to the album the songs in its subtree (including a sibling "Artwork/"
            # folder) landed in THIS run — exact, no fuzzy matching. Covers with no same-drop album
            # are dropped; the album rules / Plex supply art for any album without a local cover.
            _a = cdir
            while _a.startswith(root):
                if agg.get(_a):
                    return max(agg[_a], key=agg[_a].get)
                if _a == root:
                    break
                _a = os.path.dirname(_a)
            return None

        # ── PASS 2: covers → their album (else remove); other non-song files → remove ──
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if d != "_unnecessary"]
            for fn in fns:
                ext = os.path.splitext(fn)[1].lower()
                if ext in _AUDIO_EXTS:
                    continue                                # already handled in pass 1
                path = os.path.join(dp, fn)
                try:
                    if now - os.path.getmtime(path) < _INFLIGHT_SECS:
                        out["skipped_inflight"] += 1
                        continue
                except OSError:
                    continue
                if ext in _IMAGE_EXTS:
                    cd = _cover_dest(dp)
                    if cd:
                        out["covers"] += 1
                        _reason("cover")
                        if not dry_run:
                            try:
                                os.makedirs(cd, exist_ok=True)
                                cdst = os.path.join(cd, fn)
                                if os.path.abspath(cdst) != os.path.abspath(path):
                                    shutil.move(path, cdst)
                                    _log_recovery_move("import_cover", path, cdst)
                            except OSError:
                                log.exception("manual_import: cover move failed for %s", path)
                    else:
                        _dispose(path, "orphan_cover")      # no album anywhere → remove so the folder empties
                else:
                    _dispose(path, "non_song")

        # Remove emptied folders so the drop zone ends up EMPTY after the import.
        if not dry_run:
            for _dp, _dns, _fns in os.walk(root, topdown=False):
                if _dp == root or "_unnecessary" in _dp.split(os.sep):
                    continue
                try:
                    if not os.listdir(_dp):
                        os.rmdir(_dp)
                except OSError:
                    pass

    # Final reconciliation: re-record each imported album (idempotent set-union merge) so every
    # album is present even if a per-file commit above was swallowed. Per-file calls in Pass 1
    # already made songs live-appear in Recently Added; this is a cheap safety net (one short
    # session per album), NOT the first time these rows are written.
    if not dry_run and moved_by_album:
        for (a, b), paths in moved_by_album.items():
            _record_imported_album(a, b, paths)

    # Override the Unmatched section: every song we just placed is now present, so drop its
    # matching UnmatchedTrack row(s) (suffix-robust, artist-scoped) and reset the negative cache.
    if not dry_run and placed_titles:
        out["unmatched_resolved"] = _resolve_unmatched_for_imports(placed_titles)

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
    """Scheduler entry point. Drops wait in 'staging' until the picker is running (resumed) — so
    a paused picker leaves them staged, and resuming it organizes them onto the server."""
    if not manual_import_enabled():
        return {"skipped": "manual_import disabled"}
    if (get_config("autofill_picker_enabled", "0") or "0") != "1":
        return {"skipped": "picker paused — drops wait in staging until resumed"}
    return manual_import_scan(dry_run=_cfg_bool("manual_import_dry_run"))


# ── Async runner ────────────────────────────────────────────────────────────────────────────
# A big drop (a full discography — hundreds of FLACs, each ffmpeg-verified) takes minutes, which
# exceeds gunicorn's request timeout. The Preview/Scan buttons kick off a background thread that
# stashes its result in config; the UI polls scan_status() for the outcome.
import threading

_SCAN_LOCK = threading.Lock()


def _stash(kind: str, result: dict) -> None:
    from .db import set_config
    set_config("manual_import_last_kind", kind)
    set_config("manual_import_last_at", _dt.datetime.utcnow().isoformat() + "Z")
    set_config("manual_import_last_result_json", json.dumps(result))


def start_scan_async(dry_run: bool) -> bool:
    """Start a scan/preview in a background thread. Returns False if one is already running."""
    if not _SCAN_LOCK.acquire(blocking=False):
        return False
    from .db import set_config
    set_config("manual_import_running", "1")   # set synchronously so a poll right after start sees it
    kind = "preview" if dry_run else "scan"

    def _runner():
        try:
            _stash(kind, {"ok": True, **manual_import_scan(dry_run=dry_run)})
        except Exception as e:
            log.exception("manual_import: async %s failed", kind)
            _stash(kind, {"ok": False, "error": str(e)[:200]})
        finally:
            set_config("manual_import_running", "0")
            _SCAN_LOCK.release()

    threading.Thread(target=_runner, daemon=True, name="manual-import").start()
    return True


def scan_status() -> dict:
    from .db import get_config
    try:
        res = json.loads(get_config("manual_import_last_result_json", "") or "null")
    except Exception:
        res = None
    return {"running": (get_config("manual_import_running", "0") or "0") == "1",
            "kind": get_config("manual_import_last_kind", "") or "",
            "at": get_config("manual_import_last_at", "") or "",
            "result": res}
