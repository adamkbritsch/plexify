"""slskd_picker.py — smart album acquisition from Soulseek via slskd.

Public entry point:  acquire_album(artist, album, ...) -> dict

Never raises. Returns a result dict with at minimum:
    {success: bool, paths: list[str], peer: str|None, error: str|None,
     bytes_total: int, duration_seconds: float,
     files_attempted: int, files_completed: int}
"""
from __future__ import annotations

import logging
import os
import re
import time
import unicodedata
from typing import Optional

log = logging.getLogger(__name__)

# audio extensions we care about
_FLAC_EXT = {"flac"}
_AUDIO_EXT = {"flac", "mp3", "m4a", "alac", "aac", "ogg", "wav", "opus"}


# === Hi-res filtering ===

_HIRES_FILENAME_HINTS = (
    "24bit", "24-bit", "24 bit", "24/96", "24-96", "24_96",
    "24/192", "24-192", "24_192", "96khz", "96 khz", "96k",
    "192khz", "192 khz", "192k", "176khz", "176.4khz",
    "hires", "hi-res", "hi res", "high-res", "high res",
    "studio master", "lossless 24",
)


def _is_hires_enabled() -> bool:
    """SMART quality gate for Soulseek. We only INSIST on 24-bit hi-res from Soulseek
    peers when a hi-res source (SpotiFLAC) is actually delivering; otherwise we accept
    Soulseek's CD-quality lossless so the library keeps filling instead of rejecting
    every (perfectly good) peer file. Picky when hi-res is available elsewhere, lenient
    when Soulseek is effectively the only source.

    Hard overrides still win: autofill_allow_cd_quality=1 forces CD-OK; explicitly
    setting autofill_soulseek_hires_only=0 disables the gate entirely."""
    try:
        from .db import get_config
        if (get_config("autofill_allow_cd_quality", "0") or "0") == "1":
            return False
        if (get_config("autofill_soulseek_hires_only", "1") or "1") != "1":
            return False
        try:
            from .autofill_engine import get_provider_success_counts_1h
            if int(get_provider_success_counts_1h().get("spotiflac", 0) or 0) <= 0:
                return False  # no hi-res source delivering → accept Soulseek CD lossless
        except Exception:
            pass
        return True
    except Exception:
        return True


def _passes_hires_prefilter(slskd_file: dict, dir_path: str = "") -> bool:
    """True if the slskd file MIGHT be hi-res (24-bit ≥96 kHz).
    Tries three layers:
      1. slskd metadata: bitDepth and sampleRate fields when present
      2. bitRate heuristic: >1800 kbps suggests >CD-quality FLAC
      3. filename / parent-dir hints: '24bit', '96khz', 'hires', etc.
    Designed to RULE IN, not rule out — false positives are OK because the
    post-download verify catches them; false negatives mean we miss good files.
    """
    bd = slskd_file.get("bitDepth") or slskd_file.get("bit_depth")
    sr = slskd_file.get("sampleRate") or slskd_file.get("sample_rate")
    if isinstance(bd, (int, float)) and isinstance(sr, (int, float)):
        if bd >= 24 and sr >= 96000:
            return True
    br = slskd_file.get("bitRate") or slskd_file.get("bit_rate")
    if isinstance(br, (int, float)) and br > 1800:
        return True
    fn = (slskd_file.get("filename") or "").lower()
    dir_lower = (dir_path or "").lower()
    for hint in _HIRES_FILENAME_HINTS:
        if hint in fn or hint in dir_lower:
            return True
    return False


def _verify_hires_paths(paths: list) -> tuple[list, list]:
    """Read each FLAC via mutagen and split into (passing, failing) paths.
    Passing = 24-bit AND ≥96 kHz. Failing files have their entries returned
    for the caller to delete."""
    try:
        from mutagen.flac import FLAC
    except ImportError:
        log.warning("mutagen not available — skipping post-download hi-res verify")
        return list(paths), []
    passing, failing = [], []
    for p in paths:
        try:
            f = FLAC(p)
            bd = f.info.bits_per_sample
            sr = f.info.sample_rate
            if bd >= 24 and sr >= 96000:
                passing.append(p)
            else:
                failing.append((p, f"{bd}-bit/{sr}Hz"))
        except Exception as e:
            failing.append((p, f"mutagen read failed: {e}"))
    return passing, failing


# slskd uses backslash as path separator in filenames (Windows paths)
_SEP_RE = re.compile(r"[/\\]")

# tokens to strip when normalising album/artist names for matching
_NOISE = re.compile(
    r"\b(feat\.?|ft\.?|featuring|deluxe|edition|remastered?|remaster|"
    r"bonus|expanded|anniversary|explicit|version|vol\.?\s*\d+|disc\s*\d+|"
    r"cd\s*\d+|(19|20)\d{2}\s*(mix|remaster(ed)?)?|mix|mono|stereo|"
    r"take\s*\d+|\[.*?\]|\(.*?\))\b",
    re.IGNORECASE,
)


# normalisation helpers

def _norm(s: str) -> str:
    """Lowercase + NFKD + strip combining marks + collapse whitespace."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return " ".join(s.lower().split())


def _clean(s: str) -> str:
    """Strip noise tokens, then normalise."""
    return _norm(_NOISE.sub(" ", s or ""))


def _path_parts(filename: str) -> list:
    """Split a slskd backslash-path into components."""
    return [p for p in _SEP_RE.split(filename) if p]


def _parent_path(filename: str) -> str:
    """Return everything except the last component (the directory path)."""
    parts = _path_parts(filename)
    return "\\".join(parts[:-1]) if len(parts) >= 2 else ""


def _extension(filename: str) -> str:
    return os.path.splitext(filename)[1].lstrip(".").lower()


# query mutator

def _search_queries(artist: str, album: str,
                    sample_song: Optional[str] = None) -> list:
    """Generate 3-5 search variations, most specific first."""
    queries: list = []
    seen: set = set()

    def _add(q: str) -> None:
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            queries.append(q)

    _add(f"{artist} {album}")
    _add(f"{artist} - {album}")

    ca = _clean(artist)
    cb = _clean(album)
    if ca and cb:
        _add(f"{ca} {cb}")
        _add(f"{ca} - {cb}")

    if sample_song:
        _add(f"{artist} {sample_song}")

    return queries


# ── song-level (loosened) search ────────────────────────────────────────────
_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "to", "for", "and", "or", "with", "at",
    "by", "is", "it", "my", "me", "you", "your", "our", "we", "i", "let", "s",
    "put", "this", "that", "be", "do", "feat", "ft", "from", "out",
}


def _keywords(s: str, maxn: int = 4) -> str:
    """The distinctive words of a title (drop stopwords + short words). Soulseek matches
    loosely, so a few distinctive words recall far more than a long exact phrase."""
    out: list = []
    for w in _norm(s).split():
        if len(w) > 2 and w not in _STOPWORDS and w not in out:
            out.append(w)
    return " ".join(out[:maxn])


def _song_queries(artist: str, title: str) -> list:
    """LOOSENED, song-level search variations — from most to least specific. Searching
    PORTIONS of the song name (parentheticals dropped, distinctive words only) is what
    lets Soulseek surface the file when the exact full title finds nothing."""
    qs: list = []
    seen: set = set()

    def _add(q: str) -> None:
        q = " ".join((q or "").split())
        if q and q.lower() not in seen:
            seen.add(q.lower())
            qs.append(q)

    core = _clean(title)        # drops "(Let's Put Our)", "feat. …", brackets, etc.
    kw = _keywords(title)
    _add(f"{artist} {title}")                       # full
    if core and core != _norm(title):
        _add(f"{artist} {core}")                    # artist + core (parentheticals gone)
    _add(f"{_clean(artist)} {core}")                # cleaned both
    if kw:
        _add(f"{artist} {kw}")                      # artist + distinctive words
        _add(kw)                                    # distinctive words only (loosest)
    _add(core or _norm(title))                      # title portion only
    return [q for q in qs if q]


def _score_track_file(f: dict, artist: str, title: str,
                      upload_speed: int, has_free_slot: bool, flac_only: bool) -> float:
    """Smart, free fuzzy score for a SINGLE candidate FILE vs a target song. 0 = reject.

    Gate: the title's significant words must actually be in the filename (≥60%) AND the
    fuzzy title match must clear 70 — so we never grab a different track that happens to
    sit in the same folder. Ranking then factors format, peer speed, and a free slot."""
    from rapidfuzz.fuzz import partial_ratio, token_set_ratio
    import math
    fn = f.get("filename", "") or ""
    if f.get("isLocked"):
        return 0.0
    ext = _extension(fn)
    if flac_only and ext not in _FLAC_EXT:
        return 0.0
    if ext not in _AUDIO_EXT:
        return 0.0
    parts = _path_parts(fn)
    if not parts:
        return 0.0
    base = os.path.splitext(parts[-1])[0]
    parent = parts[-2] if len(parts) >= 2 else ""
    # DEMO/LIVE BAN: never fulfil a clean liked song with a live/demo take. The
    # SONG title bans the word 'live' anywhere (or a '(demo)') relative to the liked
    # song; the album FOLDER uses only the conservative parenthetical marker so a
    # studio album literally named "Live Through This" can't nuke its own tracks.
    from .version_match import demo_live_banned as _dlb, paren_demo_live as _pdl
    if _dlb(base, title) or (_pdl(parent) and not _pdl(title)):
        return 0.0
    base_n = _norm(base)
    hay_tokens = set(_norm(f"{parent} {base}").split())
    base_tokens = set(base_n.split())
    title_core = _clean(title) or _norm(title)
    artist_n = _norm(artist)

    # Coverage gate keys on the DISTINCTIVE words (parentheticals/stopwords dropped), so a
    # peer that named the file with just the core title (e.g. "Emotions in Motion.flac")
    # still passes, while a different song fails.
    kw = _keywords(title)
    twords = kw.split() if kw else [w for w in title_core.split() if len(w) > 2]
    if twords:
        present = sum(1 for w in twords if w in base_tokens or w in hay_tokens)
        cover = present / len(twords)
    else:
        cover = 1.0
    if cover < 0.6:
        return 0.0
    title_match = max(partial_ratio(title_core, base_n),
                      token_set_ratio(_norm(title), base_n))
    if title_match < 70:
        return 0.0
    artist_match = partial_ratio(artist_n, " ".join(hay_tokens)) if artist_n else 0
    score = float(title_match) + 0.25 * float(artist_match) + cover * 25.0
    if ext == "flac":
        score += 25.0
    if upload_speed > 0:
        score += min(120.0, math.log1p(upload_speed) * 12.0)
    if has_free_slot:
        score += 20.0
    low = base.lower()
    want = (title or "").lower()
    for w in ("instrumental", "karaoke", "live", "remix", "cover", "8d", "sped up", "slowed", "nightcore"):
        if w in low and w not in want:
            score -= 40.0
    return max(0.0, score)


# scoring

def _score_directory(
    dir_path: str,
    files: list,
    artist: str,
    album: str,
    expected_track_count,
    flac_only: bool,
    peer_upload_speed: int,
    peer_has_free_slot: bool,
) -> float:
    """Score a candidate directory (0 = unusable, higher = better)."""
    from rapidfuzz.fuzz import partial_ratio, token_sort_ratio

    ext_ok = _FLAC_EXT if flac_only else _AUDIO_EXT
    audio_files = [f for f in files
                   if _extension(f.get("filename", "")) in ext_ok
                   and not f.get("isLocked", False)]
    # Hi-res pre-filter — keep only files that MIGHT be 24-bit ≥96 kHz
    if _is_hires_enabled():
        audio_files = [f for f in audio_files if _passes_hires_prefilter(f, dir_path)]

    if not audio_files:
        return 0.0

    # Reject single-file bundles >100 MB
    if len(audio_files) == 1 and audio_files[0].get("size", 0) > 100 * 1024 * 1024:
        return 0.0
    # Reject implausibly large directories (recycle bins, full library dumps)
    # If we have an expected count, the 3× cap above handles it.
    # Otherwise cap at 60 files — no album has more than ~60 tracks.
    if not expected_track_count and len(audio_files) > 60:
        return 0.0

    score = float(len(audio_files)) * 10.0

    if expected_track_count:
        diff = abs(len(audio_files) - expected_track_count)
        if diff == 0:
            score += 50.0
        elif diff <= 2:
            score += 20.0
        elif diff <= 4:
            score += 5.0
        elif diff <= 8:
            score -= float(diff) * 5.0
        else:
            # Heavily penalise directories that are wildly over/under count
            # (catches recycle-bins and huge library dumps)
            score -= float(diff) * 20.0
    # Absolute sanity cap: reject directories with > 3× expected track count
    if expected_track_count and len(audio_files) > expected_track_count * 3:
        return 0.0

    dir_name = os.path.basename(dir_path) if dir_path else ""
    dir_norm = _norm(dir_name)
    artist_norm = _clean(artist)
    album_norm = _clean(album)
    combined_target = f"{artist_norm} {album_norm}"

    name_score = max(
        partial_ratio(album_norm, dir_norm),
        token_sort_ratio(combined_target, dir_norm),
    )
    score += name_score * 0.5

    if peer_upload_speed > 0:
        import math
        score += min(200.0, math.log1p(peer_upload_speed) * 15.0)

    if peer_has_free_slot:
        score += 30.0

    return score


# candidate extraction from search responses

def _extract_candidates(
    responses: list,
    artist: str,
    album: str,
    expected_track_count,
    flac_only: bool,
) -> list:
    """
    For each peer response, group files by parent directory and score each dir.
    Returns sorted list of {peer, dir_path, files, score, upload_speed} dicts.
    Skips peers with no free upload slots.
    """
    candidates: list = []

    for resp in responses:
        peer = resp.get("username", "")
        if not peer:
            continue
        upload_speed = resp.get("uploadSpeed") or 0
        has_free_slot = bool(resp.get("hasFreeUploadSlot"))

        if not has_free_slot:
            continue

        files = resp.get("files") or []
        dirs: dict = {}
        for f in files:
            parent = _parent_path(f.get("filename", ""))
            dirs.setdefault(parent, []).append(f)

        for dir_path, dir_files in dirs.items():
            sc = _score_directory(
                dir_path, dir_files, artist, album,
                expected_track_count, flac_only,
                upload_speed, has_free_slot,
            )
            if sc > 0:
                candidates.append({
                    "peer": peer,
                    "dir_path": dir_path,
                    "files": dir_files,
                    "score": sc,
                    "upload_speed": upload_speed,
                })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates


# browse augmentation

def _browse_augment(
    peer: str,
    current_dir_path: str,
    current_files: list,
    artist: str,
    album: str,
    expected_track_count: int,
    flac_only: bool,
    upload_speed: int,
) -> tuple:
    """Browse the peer's full library to find a better-matching directory.
    Returns (dir_path, files) — either the original or a replacement.
    """
    from . import slskd_client
    log.info("slskd_picker: browsing %s to look for better match", peer)
    try:
        dirs = slskd_client.browse_user(peer)
    except Exception:
        return current_dir_path, current_files

    best_path = current_dir_path
    best_files = current_files
    best_score = _score_directory(
        current_dir_path, current_files, artist, album,
        expected_track_count, flac_only, upload_speed, True,
    )

    for d in dirs:
        dir_path = d.get("dirname") or d.get("name") or ""
        raw_files = d.get("files") or []
        adapted: list = []
        for f in raw_files:
            fn = f.get("filename") or f.get("name") or ""
            adapted.append({
                "filename": f"{dir_path}\\{fn}",
                "size": f.get("size", 0),
                "isLocked": False,
            })
        sc = _score_directory(
            dir_path, adapted, artist, album,
            expected_track_count, flac_only, upload_speed, True,
        )
        if sc > best_score:
            best_score = sc
            best_path = dir_path
            best_files = adapted

    if best_path != current_dir_path:
        log.info("slskd_picker: browse improved dir from %r to %r (score %.1f)",
                 current_dir_path, best_path, best_score)
    return best_path, best_files


# download + poll

def _enqueue_and_wait(
    peer: str,
    files: list,
    download_dir: str,
    flac_only: bool,
    single_track_ok: bool,
    timeout_seconds: int,
    start_time: float,
    enforce_hires: Optional[bool] = None,
) -> dict:
    """Enqueue files for download and poll until complete or timeout."""
    from . import slskd_client

    ext_ok = _FLAC_EXT if flac_only else _AUDIO_EXT
    target_files = [f for f in files
                    if _extension(f.get("filename", "")) in ext_ok
                    and not f.get("isLocked", False)]

    if not single_track_ok:
        target_files = [f for f in target_files
                        if not (len(target_files) == 1
                                and f.get("size", 0) > 100 * 1024 * 1024)]

    if not target_files:
        return {"success": False, "error": "no eligible files after filtering",
                "files_attempted": 0, "files_completed": 0, "paths": []}

    log.info("slskd_picker: enqueueing %d files from %s", len(target_files), peer)
    enqueued_names: set = set()
    for f in target_files:
        fname = f.get("filename", "")
        size = f.get("size", 0)
        ok = slskd_client.enqueue_download(peer, fname, size)
        if ok:
            enqueued_names.add(fname)
        else:
            log.warning("slskd_picker: failed to enqueue %r from %s", fname[-60:], peer)

    if not enqueued_names:
        return {"success": False, "error": "all enqueues rejected",
                "files_attempted": len(target_files), "files_completed": 0, "paths": []}

    completed_paths: list = []
    poll_interval = 5.0
    last_completed = 0
    stall_count = 0

    while True:
        elapsed = time.monotonic() - start_time
        remaining = timeout_seconds - elapsed
        if remaining <= 0:
            log.warning("slskd_picker: timed out waiting for downloads from %s", peer)
            break

        try:
            transfers = slskd_client.get_transfers_for_user(peer)
        except Exception as e:
            log.warning("slskd_picker: transfer poll raised: %s", e)
            time.sleep(poll_interval)
            continue

        state_map: dict = {}
        for t in transfers:
            parts = _path_parts(t.get("filename", ""))
            basename = parts[-1] if parts else ""
            if basename:
                state_map[basename] = t.get("state", "")

        done_count = 0
        pending_count = 0
        active_count = 0  # in-progress or queued (not stalled)
        for fname in enqueued_names:
            parts = _path_parts(fname)
            basename = parts[-1] if parts else ""
            state = state_map.get(basename, "")
            if any(k in state for k in ("Succeeded", "Completed")):
                done_count += 1
            elif any(k in state for k in ("Failed", "Cancelled", "Rejected")):
                done_count += 1
            elif any(k in state for k in ("InProgress", "Queued")):
                pending_count += 1
                active_count += 1
            else:
                pending_count += 1  # unknown state — still pending

        # Reset stall counter if we see progress (more done OR still active transfers)
        if done_count > last_completed or active_count > 0:
            last_completed = done_count
            stall_count = 0
        else:
            stall_count += 1

        log.debug("slskd_picker: %d/%d done, %d pending (stall=%d)",
                  done_count, len(enqueued_names), pending_count, stall_count)

        if pending_count == 0:
            log.info("slskd_picker: all %d transfers done", done_count)
            break

        if stall_count >= 20:
            log.warning("slskd_picker: stalled (%d polls with no progress) waiting for %s — bailing",
                        stall_count, peer)
            break

        time.sleep(max(0.5, min(poll_interval, remaining - 1)))

    completed_paths = _find_delivered_files(
        peer, enqueued_names, download_dir, flac_only
    )

    # hi-res post-verify: read each FLAC; delete files that aren't 24-bit ≥96 kHz.
    # No-op when autofill_soulseek_hires_only is disabled (config default '1' = on).
    hires_rejected = 0
    _do_hires = _is_hires_enabled() if enforce_hires is None else enforce_hires
    if _do_hires and completed_paths:
        passing, failing = _verify_hires_paths(completed_paths)
        if failing:
            log.info("acquire_album: hi-res verify rejected %d/%d files",
                     len(failing), len(completed_paths))
            for p, reason in failing:
                log.info("  reject %s (%s)", os.path.basename(p), reason)
                try:
                    os.remove(p)
                except OSError:
                    pass
            hires_rejected = len(failing)
            completed_paths = passing

    # L-6 + post-incident-sweep: require ALL enqueued files complete. Half-finished
    # albums in /Volumes/MediaVolume3/plexify-music cause Plex to show broken albums; user must manually re-queue.
    # Single-track-OK callers still accept any 1 file (enqueued_names will be 1).
    _required = max(1, len(enqueued_names))  # was len(enqueued_names) // 2
    _is_success = bool(completed_paths) and len(completed_paths) >= _required
    return {
        "success": _is_success,
        "paths": completed_paths,
        "files_attempted": len(enqueued_names),
        "files_completed": len(completed_paths),
        "hires_rejected": hires_rejected,
        "error": None if completed_paths else (
            f"hi-res verify rejected all {hires_rejected} files" if hires_rejected
            else "no files found in download_dir after transfer"
        ),
    }


def _find_delivered_files(
    peer: str,
    enqueued_names: set,
    download_dir: str,
    flac_only: bool,
) -> list:
    """Walk download_dir (and slskd default /downloads/music) for delivered files."""
    ext_ok = _FLAC_EXT if flac_only else _AUDIO_EXT
    wanted_basenames = {
        _path_parts(fn)[-1].lower()
        for fn in enqueued_names
        if _path_parts(fn)
    }

    # slskd writes finished downloads to /downloads/music/complete/{dirname}/
    # From Plexify's volume mount that is /Volumes/MediaVolume3/Downloads/music/complete/{dirname}/
    # The download_dir (spotiflac) is where SpotiFLAC puts things; slskd uses /complete.
    # We search all of them so we find the files wherever they landed.
    search_roots = [
        download_dir,
        "/Volumes/MediaVolume3/Downloads/music/complete",
        "/Volumes/MediaVolume3/Downloads/music/incomplete",
        f"/downloads/music/{peer}",
        "/downloads/music",
    ]

    found: list = []
    seen: set = set()
    for root in search_roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if fn.lower() in wanted_basenames and _extension(fn) in ext_ok:
                    abs_path = os.path.join(dirpath, fn)
                    if abs_path not in seen:
                        seen.add(abs_path)
                        found.append(abs_path)
    return found


# public entry point

def configured() -> bool:
    """True if slskd_client is configured."""
    try:
        from . import slskd_client
        return slskd_client.configured()
    except Exception:
        return False


def acquire_album(
    artist: str,
    album: str,
    sample_song: Optional[str] = None,
    single_track_ok: bool = False,
    expected_track_count: Optional[int] = None,
    download_dir: str = "/Volumes/MediaVolume3/Downloads/music/spotiflac",
    flac_only: bool = True,
    timeout_seconds: int = 600,
) -> dict:
    """Smart-acquire an album from Soulseek and stage files to download_dir.

    Returns dict:
        {success: bool, paths: list[str], peer: str|None, error: str|None,
         bytes_total: int, duration_seconds: float,
         files_attempted: int, files_completed: int}
    Never raises.
    """
    from . import slskd_client

    start = time.monotonic()

    def _elapsed() -> float:
        return time.monotonic() - start

    def _ret(success: bool, paths=None, peer=None,
             error=None, files_attempted: int = 0,
             files_completed: int = 0) -> dict:
        paths = paths or []
        total = sum(os.path.getsize(p) for p in paths if os.path.exists(p))
        return {
            "success": success, "paths": paths, "peer": peer, "error": error,
            "bytes_total": total, "duration_seconds": round(_elapsed(), 2),
            "files_attempted": files_attempted,
            "files_completed": files_completed,
        }

    try:
        if not configured():
            return _ret(False, error="slskd not configured")

        st = slskd_client.check_status()
        if st is None or not st.logged_in:
            return _ret(False, error="slskd not logged in to Soulseek")

        log.info("slskd_picker: acquire_album artist=%r album=%r flac_only=%s timeout=%ds",
                 artist, album, flac_only, timeout_seconds)

        queries = _search_queries(artist, album, sample_song)
        all_responses: list = []

        for q in queries:
            if _elapsed() > timeout_seconds * 0.4:
                log.info("slskd_picker: 40%% of timeout elapsed, stopping search phase")
                break
            log.info("slskd_picker: searching %r", q)
            sid = slskd_client.search(q)
            if not sid:
                continue
            wait = min(100.0, (timeout_seconds * 0.4) - _elapsed())  # was 20s — too short, slskd needs ~90s
            if wait <= 2:
                break
            responses = slskd_client.get_search_results(sid, wait_seconds=wait)
            log.info("slskd_picker: query %r got %d peer responses", q, len(responses))
            all_responses.extend(responses)

        if not all_responses:
            return _ret(False, error="no responses from any search query")

        # Deduplicate by username, keep last seen (later queries may refine)
        deduped: dict = {}
        for resp in all_responses:
            peer_name = resp.get("username", "")
            if peer_name:
                deduped[peer_name] = resp
        all_responses = list(deduped.values())

        candidates = _extract_candidates(
            all_responses, artist, album, expected_track_count, flac_only
        )

        if not candidates and flac_only:
            log.info("slskd_picker: no FLAC candidates, trying any audio format")
            candidates = _extract_candidates(
                all_responses, artist, album, expected_track_count, False
            )

        if not candidates:
            return _ret(False, error="no viable candidates found in search results")

        best = candidates[0]
        best_peer = best["peer"]
        best_dir = best["dir_path"]
        best_files = best["files"]
        best_speed = best["upload_speed"]

        log.info("slskd_picker: best candidate peer=%s dir=%r files=%d score=%.1f",
                 best_peer,
                 best_dir[-60:] if best_dir else "",
                 len(best_files),
                 best["score"])

        # Browse augmentation if we have fewer files than expected
        if (expected_track_count
                and len(best_files) < expected_track_count - 2
                and _elapsed() < timeout_seconds * 0.5):
            best_dir, best_files = _browse_augment(
                best_peer, best_dir, best_files,
                artist, album, expected_track_count, flac_only, best_speed,
            )

        dl_result = _enqueue_and_wait(
            peer=best_peer,
            files=best_files,
            download_dir=download_dir,
            flac_only=flac_only,
            single_track_ok=single_track_ok,
            timeout_seconds=timeout_seconds,
            start_time=start,
        )

        return _ret(
            success=dl_result["success"],
            paths=dl_result.get("paths") or [],
            peer=best_peer,
            error=dl_result.get("error"),
            files_attempted=dl_result.get("files_attempted", 0),
            files_completed=dl_result.get("files_completed", 0),
        )

    except Exception as e:
        log.exception("slskd_picker: acquire_album raised unexpectedly")
        return _ret(False, error=f"unexpected error: {e}")


def acquire_track(
    artist: str,
    title: str,
    download_dir: str = "/Volumes/MediaVolume3/Downloads/music/spotiflac",
    flac_only: bool = True,
    timeout_seconds: int = 180,
) -> dict:
    """Song-level Soulseek acquire: LOOSENED search (portions of the song name) + a free
    fuzzy 'smart matcher' that analyses the whole result list and picks the best matching
    FILE (not directory), then downloads just that one. Returns the acquire_album shape.
    Never raises."""
    from . import slskd_client
    start = time.monotonic()

    def _elapsed() -> float:
        return time.monotonic() - start

    def _ret(success, paths=None, peer=None, error=None):
        paths = paths or []
        total = sum(os.path.getsize(p) for p in paths if os.path.exists(p))
        return {"success": success, "paths": paths, "peer": peer, "error": error,
                "bytes_total": total, "duration_seconds": round(_elapsed(), 2),
                "files_attempted": 1 if paths or success else 0,
                "files_completed": len(paths)}

    try:
        if not configured():
            return _ret(False, error="slskd not configured")
        st = slskd_client.check_status()
        if st is None or not st.logged_in:
            return _ret(False, error="slskd not logged in to Soulseek")

        log.info("slskd_picker: acquire_track artist=%r title=%r flac_only=%s", artist, title, flac_only)
        responses: list = []
        for q in _song_queries(artist, title):
            if _elapsed() > timeout_seconds * 0.5:
                break
            log.info("slskd_picker: track-search %r", q)
            sid = slskd_client.search(q)
            if not sid:
                continue
            wait = min(60.0, (timeout_seconds * 0.5) - _elapsed())
            if wait <= 2:
                break
            responses.extend(slskd_client.get_search_results(sid, wait_seconds=wait))
        if not responses:
            return _ret(False, error="no responses from track search")

        deduped: dict = {}
        for r in responses:
            u = r.get("username", "")
            if u:
                deduped[u] = r

        def _best(strict_flac: bool, require_slot: bool = True):
            best = None  # (score, peer, file)
            for r in deduped.values():
                peer = r.get("username", "")
                if not peer:
                    continue
                has_slot = bool(r.get("hasFreeUploadSlot"))
                if require_slot and not has_slot:
                    continue
                speed = r.get("uploadSpeed") or 0
                qlen = r.get("queueLength") or 0
                for f in (r.get("files") or []):
                    sc = _score_track_file(f, artist, title, speed, has_slot, strict_flac)
                    if not require_slot:
                        # busy peer: slskd queues remotely just fine — prefer the
                        # shortest remote queue among otherwise-equal matches.
                        sc -= min(50.0, float(qlen))
                    if sc > 0 and (best is None or sc > best[0]):
                        best = (sc, peer, f)
            return best

        # Free-slot peers first; if EVERY peer holding the track is busy (common
        # for popular tracks), fall back to queueing on a busy peer instead of
        # failing with "no confident file match".
        best = _best(flac_only) or _best(flac_only, require_slot=False)
        if best is None and flac_only:
            best = _best(False) or _best(False, require_slot=False)
        if best is None:
            return _ret(False, error="no confident file match in results")
        sc, peer, f = best
        log.info("slskd_picker: track best score=%.1f peer=%s file=%r",
                 sc, peer, (f.get("filename", "") or "")[-70:])
        _is_flac = _extension(f.get("filename", "")) == "flac"
        dl = _enqueue_and_wait(
            peer=peer, files=[f], download_dir=download_dir,
            flac_only=_is_flac, single_track_ok=True,
            timeout_seconds=timeout_seconds, start_time=start, enforce_hires=False,
        )
        return _ret(dl.get("success", False), paths=dl.get("paths") or [],
                    peer=peer, error=dl.get("error"))
    except Exception as e:
        log.exception("slskd_picker: acquire_track raised unexpectedly")
        return _ret(False, error=f"unexpected error: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# NAS-DAEMON DELEGATION (Mac split). On the Mac, the download itself runs on
# the NAS plexify-downloader (:8788); these overrides keep the exact signature
# + return shape of the real implementations above, so picker_tick and every
# other caller is untouched. The implementations above still run verbatim
# inside the daemon on the NAS. (engine/ holds the pristine copy.)
# ═══════════════════════════════════════════════════════════════════════════

def acquire_album(artist, album, sample_song=None, single_track_ok=False,
                  expected_track_count=None,
                  download_dir="/Volumes/MediaVolume3/Downloads/music/spotiflac",
                  flac_only=True, timeout_seconds=600):
    from .nas_downloader import enqueue_and_wait
    r = enqueue_and_wait("soulseek", mode="album", dest_dir=download_dir,
                         artist=artist, album=album,
                         kwargs={"sample_song": sample_song,
                                 "single_track_ok": single_track_ok,
                                 "expected_track_count": expected_track_count,
                                 "flac_only": flac_only,
                                 "timeout_seconds": timeout_seconds},
                         timeout_seconds=timeout_seconds)
    r.setdefault("files_attempted", len(r.get("paths") or []))
    r.setdefault("hires_rejected", 0)
    return r


def acquire_track(artist, title,
                  download_dir="/Volumes/MediaVolume3/Downloads/music/spotiflac",
                  flac_only=True, timeout_seconds=180):
    from .nas_downloader import enqueue_and_wait
    r = enqueue_and_wait("soulseek", mode="track", dest_dir=download_dir,
                         artist=artist, title=title,
                         kwargs={"flac_only": flac_only,
                                 "timeout_seconds": timeout_seconds},
                         timeout_seconds=timeout_seconds)
    r.setdefault("files_attempted", len(r.get("paths") or []))
    return r
