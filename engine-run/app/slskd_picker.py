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

# junk-version markers, WORD-BOUNDED (see _score_track_file): 'live' must not match
# Alive/Oliver, 'cover' must not match Undercover/Recovered, etc.
_JUNK_RE = re.compile(
    r"\b(instrumental|karaoke|live|remix|cover|8d|sped[\s_-]?up|slowed|nightcore)\b",
    re.IGNORECASE,
)

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
    """Generate search variations, HIGHEST-RECALL first.

    Soulseek only returns a file when every query term appears in its path, so long raw
    titles ('HIStory - PAST, PRESENT AND FUTURE - BOOK I') match nothing while the
    cleaned/keyword forms match everything (audit 2026-07-05, S9/S9b: the raw-first
    ordering + serial time budget meant the variants that WOULD hit often never ran).
    Order: cleaned artist+album → keyword form → raw (still useful for exact-named
    dirs when short) → sample-song rescue."""
    queries: list = []
    seen: set = set()

    def _add(q: str) -> None:
        q = " ".join((q or "").split())
        if q and q.lower() not in seen:
            seen.add(q.lower())
            queries.append(q)

    ca = _clean(artist)
    cb = _clean(album)
    kw_album = _keywords(album, maxn=4)

    if ca and cb and len(cb.split()) <= 6:
        _add(f"{ca} {cb}")                      # cleaned — what peers' folders look like
    if kw_album:
        _add(f"{ca or artist} {kw_album}")      # distinctive words only (high recall)
    if len(f"{artist} {album}") < 50:
        _add(f"{artist} {album}")               # raw exact (short titles only — every raw
        _add(f"{artist} - {album}")             # term must appear in the peer's path;
                                                # raw also catches accent-named shares
                                                # that the cleaned form misses)
    kw1 = _keywords(album, maxn=1)
    if kw1:
        _add(f"{ca or artist} {kw1}")           # single-keyword backstop: hits shares
                                                # named just 'Artist\HIStory\...' where
                                                # the full subtitle never appears
    if sample_song:
        _add(f"{artist} {sample_song}")
        sk = _keywords(sample_song, maxn=3)
        if sk:
            _add(f"{ca or artist} {sk}")

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
    # Junk-version penalty with WORD BOUNDARIES — raw substring matching wrongly hit
    # 'live' in Alive/Oliver/Delivered, 'cover' in Undercover Martyn/Recovered, '8d' in
    # peer hash suffixes, killing perfectly good files (audit 2026-07-05, S4a).
    _want_junk = {m.lower() for m in _JUNK_RE.findall(title or "")}
    for m in _JUNK_RE.findall(base):
        if m.lower() not in _want_junk:
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
    # Hi-res pre-filter — PREFER files that look 24-bit ≥96 kHz, but don't erase the
    # whole directory when none carry hints: plenty of genuine hi-res rips have plain
    # names, and the post-download mutagen verify is the real enforcement (audit
    # 2026-07-05, S7 — the hard filter silently discarded viable dirs).
    hires_bonus = 0.0
    if _is_hires_enabled():
        hinted = [f for f in audio_files if _passes_hires_prefilter(f, dir_path)]
        if hinted:
            audio_files = hinted
            hires_bonus = 40.0
        # else: keep the unhinted dir as a lower-confidence candidate

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

    score = float(len(audio_files)) * 10.0 + hires_bonus

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
    Returns sorted list of {peer, dir_path, files, score, upload_speed, has_free_slot}.

    Audit 2026-07-05: (a) responses are MERGED per peer across queries first — the old
    'last response per peer wins' dedup dropped files a peer only surfaced for an earlier
    query (S5); (b) peers with no free upload slot are RANKED DOWN, not skipped — slskd
    queues remotely just fine, and hard-skipping made popular albums (all peers busy)
    structurally unacquirable (S3; the track path already had this fallback).
    """
    # merge responses per peer: union of files (by filename), best speed/slot flags
    merged: dict = {}
    for resp in responses:
        peer = resp.get("username", "")
        if not peer:
            continue
        m = merged.setdefault(peer, {"speed": 0, "slot": False, "queue": 0, "files": {}})
        m["speed"] = max(m["speed"], resp.get("uploadSpeed") or 0)
        m["slot"] = m["slot"] or bool(resp.get("hasFreeUploadSlot"))
        m["queue"] = max(m["queue"], resp.get("queueLength") or 0)
        for f in (resp.get("files") or []):
            fn = f.get("filename", "")
            if fn:
                m["files"].setdefault(fn, f)

    candidates: list = []
    for peer, m in merged.items():
        dirs: dict = {}
        for f in m["files"].values():
            parent = _parent_path(f.get("filename", ""))
            dirs.setdefault(parent, []).append(f)

        for dir_path, dir_files in dirs.items():
            sc = _score_directory(
                dir_path, dir_files, artist, album,
                expected_track_count, flac_only,
                m["speed"], m["slot"],
            )
            if sc > 0:
                if not m["slot"]:
                    # busy peer: usable, but prefer free slots; shorter remote queue wins
                    sc -= 60.0 + min(60.0, float(m["queue"]))
                if sc <= 0:
                    continue
                candidates.append({
                    "peer": peer,
                    "dir_path": dir_path,
                    "files": dir_files,
                    "score": sc,
                    "upload_speed": m["speed"],
                    "has_free_slot": m["slot"],
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

        # Audit 2026-07-05 (S6b): 'Completed, Rejected' contains BOTH 'Completed' and
        # 'Rejected' — the old check counted a rejected 0-byte transfer as generically
        # 'done' with no distinction. Track failures separately: the moment every
        # transfer has RESOLVED (succeeded or failed) we exit — a failed peer now costs
        # seconds, not a 100s stall window, which is what makes candidate failover cheap.
        ok_count = 0
        fail_count = 0
        pending_count = 0
        active_count = 0  # in-progress or queued (not stalled)
        for fname in enqueued_names:
            parts = _path_parts(fname)
            basename = parts[-1] if parts else ""
            state = state_map.get(basename, "")
            if any(k in state for k in ("Failed", "Cancelled", "Rejected", "Errored", "TimedOut")):
                fail_count += 1
            elif any(k in state for k in ("Succeeded", "Completed")):
                ok_count += 1
            elif any(k in state for k in ("InProgress", "Queued")):
                pending_count += 1
                active_count += 1
            else:
                pending_count += 1  # unknown state — still pending
        done_count = ok_count + fail_count

        # Reset stall counter if we see progress (more done OR still active transfers)
        if done_count > last_completed or active_count > 0:
            last_completed = done_count
            stall_count = 0
        else:
            stall_count += 1

        log.debug("slskd_picker: %d ok + %d failed / %d, %d pending (stall=%d)",
                  ok_count, fail_count, len(enqueued_names), pending_count, stall_count)

        if pending_count == 0:
            log.info("slskd_picker: all %d transfers resolved (%d ok, %d failed)",
                     done_count, ok_count, fail_count)
            break

        if stall_count >= 12:
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
    # slskd flips a transfer to 'Completed, Succeeded' a beat BEFORE the
    # incomplete/ -> complete/ relocation lands on disk, so a walk right at
    # completion can miss the file (observed live 2026-07-01). Retry briefly.
    for _attempt in range(4):
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
        if found:
            break
        time.sleep(2.0)
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

        # ── SEARCH PHASE (audit 2026-07-05, S1): short per-query waits + early stop.
        # get_search_results early-exits on isComplete, so a 30s cap is a ceiling, not a
        # sleep — EVERY query variant now actually runs (the old min(100s,…) waits let
        # query #1 eat the whole budget so the high-recall variants never executed).
        # Stop early once a strong candidate pool exists.
        queries = _search_queries(artist, album, sample_song)
        all_responses: list = []
        candidates: list = []
        search_budget = timeout_seconds * 0.5

        for q in queries:
            remaining_budget = search_budget - _elapsed()
            if remaining_budget <= 4:
                log.info("slskd_picker: search budget elapsed, stopping search phase")
                break
            log.info("slskd_picker: searching %r", q)
            sid = slskd_client.search(q)
            if not sid:
                continue
            responses = slskd_client.get_search_results(
                sid, wait_seconds=min(30.0, remaining_budget))
            log.info("slskd_picker: query %r got %d peer responses", q, len(responses))
            all_responses.extend(responses)
            # Re-rank after each query; stop searching once we have a clearly good dir
            # (strong score + plausible track count) — recall with no wasted wall-clock.
            candidates = _extract_candidates(
                all_responses, artist, album, expected_track_count, flac_only)
            if candidates:
                top = candidates[0]
                enough = (len(top["files"]) >= (expected_track_count or 3) - 2
                          if expected_track_count else len(top["files"]) >= 3)
                if top["score"] >= 250.0 and top.get("has_free_slot") and enough:
                    log.info("slskd_picker: strong candidate found early (score %.1f) — "
                             "stopping search phase", top["score"])
                    break

        if not all_responses:
            return _ret(False, error="no responses from any search query")

        if not candidates and flac_only:
            log.info("slskd_picker: no FLAC candidates, trying any audio format")
            candidates = _extract_candidates(
                all_responses, artist, album, expected_track_count, False
            )

        if not candidates:
            return _ret(False, error="no viable candidates found in search results")

        # ── DOWNLOAD PHASE (audit 2026-07-05, S2): FAILOVER across top candidates.
        # One flaky peer ('Completed, Rejected', 0 bytes — a known live failure) used to
        # kill the whole acquisition even when other peers had the album. Try up to 3
        # candidate dirs on DISTINCT peers, best first.
        tried_peers: set = set()
        last_error = None
        attempted_total = 0
        for cand in candidates:
            if len(tried_peers) >= 3:
                break
            if cand["peer"] in tried_peers:
                continue
            if _elapsed() > timeout_seconds - 30:
                last_error = last_error or "timeout before all candidates tried"
                break
            tried_peers.add(cand["peer"])
            best_peer = cand["peer"]
            best_dir = cand["dir_path"]
            best_files = cand["files"]
            best_speed = cand["upload_speed"]

            log.info("slskd_picker: candidate #%d peer=%s dir=%r files=%d score=%.1f",
                     len(tried_peers), best_peer,
                     best_dir[-60:] if best_dir else "",
                     len(best_files), cand["score"])

            # Browse augmentation if we have fewer files than expected
            if (expected_track_count
                    and len(best_files) < expected_track_count - 2
                    and _elapsed() < timeout_seconds * 0.6):
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
            attempted_total += dl_result.get("files_attempted", 0)

            if dl_result["success"]:
                return _ret(
                    success=True,
                    paths=dl_result.get("paths") or [],
                    peer=best_peer,
                    error=None,
                    files_attempted=dl_result.get("files_attempted", 0),
                    files_completed=dl_result.get("files_completed", 0),
                )

            last_error = dl_result.get("error") or "download failed"
            log.info("slskd_picker: candidate peer=%s failed (%s) — trying next",
                     best_peer, last_error)
            # Best-effort: clear our queued transfers on the failed peer so they don't
            # deliver hours later into the downloads dir unattended.
            try:
                slskd_client.cancel_downloads_for_user(best_peer)
            except Exception:
                pass

        return _ret(False, peer=None,
                    error=f"all {len(tried_peers)} candidate peers failed: {last_error}",
                    files_attempted=attempted_total)

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
        # SEARCH (audit 2026-07-05, S1): short waits — get_search_results early-exits on
        # isComplete, so every loosened variant actually runs. Stop once a strong file
        # match exists instead of burning the whole budget.
        responses: list = []
        search_budget = timeout_seconds * 0.6
        for q in _song_queries(artist, title):
            remaining_budget = search_budget - _elapsed()
            if remaining_budget <= 4:
                break
            log.info("slskd_picker: track-search %r", q)
            sid = slskd_client.search(q)
            if not sid:
                continue
            responses.extend(slskd_client.get_search_results(
                sid, wait_seconds=min(25.0, remaining_budget)))
            # quick strength probe: a ≥200-score free-slot FLAC is plenty — stop searching
            _probe = None
            for r in responses:
                _slot = bool(r.get("hasFreeUploadSlot"))
                if not _slot:
                    continue
                _speed = r.get("uploadSpeed") or 0
                for f in (r.get("files") or []):
                    _sc = _score_track_file(f, artist, title, _speed, _slot, True)
                    if _probe is None or _sc > _probe:
                        _probe = _sc
            if _probe and _probe >= 200.0:
                log.info("slskd_picker: strong track match found early (%.1f) — stopping search", _probe)
                break
        if not responses:
            return _ret(False, error="no responses from track search")

        # Merge per peer across queries (S5) — union the file lists.
        merged: dict = {}
        for r in responses:
            u = r.get("username", "")
            if not u:
                continue
            m = merged.setdefault(u, {"speed": 0, "slot": False, "queue": 0, "files": {}})
            m["speed"] = max(m["speed"], r.get("uploadSpeed") or 0)
            m["slot"] = m["slot"] or bool(r.get("hasFreeUploadSlot"))
            m["queue"] = max(m["queue"], r.get("queueLength") or 0)
            for f in (r.get("files") or []):
                fn = f.get("filename", "")
                if fn:
                    m["files"].setdefault(fn, f)

        def _ranked(strict_flac: bool, require_slot: bool = True):
            out = []  # (score, peer, file)
            for peer, m in merged.items():
                if require_slot and not m["slot"]:
                    continue
                for f in m["files"].values():
                    sc = _score_track_file(f, artist, title, m["speed"], m["slot"], strict_flac)
                    if not require_slot and not m["slot"]:
                        # busy peer: slskd queues remotely just fine — prefer the
                        # shortest remote queue among otherwise-equal matches.
                        sc -= min(50.0, float(m["queue"]))
                    if sc > 0:
                        out.append((sc, peer, f))
            out.sort(key=lambda t: t[0], reverse=True)
            return out

        # Free-slot peers first; if EVERY peer holding the track is busy (common
        # for popular tracks), fall back to queueing on a busy peer instead of
        # failing with "no confident file match".
        ranked = _ranked(flac_only) or _ranked(flac_only, require_slot=False)
        if not ranked and flac_only:
            ranked = _ranked(False) or _ranked(False, require_slot=False)
        if not ranked:
            return _ret(False, error="no confident file match in results")

        # FAILOVER (audit 2026-07-05, S2): try the top matches on DISTINCT peers — one
        # rejecting peer ('Completed, Rejected', 0 bytes) no longer sinks the acquire.
        tried_peers: set = set()
        last_error = None
        for sc, peer, f in ranked:
            if len(tried_peers) >= 3:
                break
            if peer in tried_peers:
                continue
            if _elapsed() > timeout_seconds - 20:
                last_error = last_error or "timeout before all candidates tried"
                break
            tried_peers.add(peer)
            log.info("slskd_picker: track candidate #%d score=%.1f peer=%s file=%r",
                     len(tried_peers), sc, peer, (f.get("filename", "") or "")[-70:])
            _is_flac = _extension(f.get("filename", "")) == "flac"
            dl = _enqueue_and_wait(
                peer=peer, files=[f], download_dir=download_dir,
                flac_only=_is_flac, single_track_ok=True,
                timeout_seconds=timeout_seconds, start_time=start, enforce_hires=False,
            )
            if dl.get("success"):
                return _ret(True, paths=dl.get("paths") or [], peer=peer)
            last_error = dl.get("error") or "download failed"
            log.info("slskd_picker: track candidate peer=%s failed (%s) — trying next",
                     peer, last_error)
            try:
                slskd_client.cancel_downloads_for_user(peer)
            except Exception:
                pass
        return _ret(False, error=f"all {len(tried_peers)} track candidates failed: {last_error}")
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

# The near-storage implementations above are what the NAS daemon actually runs; the
# delegating overrides below shadow their public names for the Mac engine. Capture the
# real ones first so the daemon (PLEXIFY_DOWNLOADER_DAEMON=1) can still reach them.
_real_acquire_album = acquire_album
_real_acquire_track = acquire_track


def acquire_album(artist, album, sample_song=None, single_track_ok=False,
                  expected_track_count=None,
                  download_dir="/Volumes/MediaVolume3/Downloads/music/spotiflac",
                  flac_only=True, timeout_seconds=600):
    if os.environ.get("PLEXIFY_DOWNLOADER_DAEMON") == "1":
        return _real_acquire_album(artist, album, sample_song=sample_song,
                                   single_track_ok=single_track_ok,
                                   expected_track_count=expected_track_count,
                                   download_dir=download_dir, flac_only=flac_only,
                                   timeout_seconds=timeout_seconds)
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
    if os.environ.get("PLEXIFY_DOWNLOADER_DAEMON") == "1":
        return _real_acquire_track(artist, title, download_dir=download_dir,
                                   flac_only=flac_only, timeout_seconds=timeout_seconds)
    from .nas_downloader import enqueue_and_wait
    r = enqueue_and_wait("soulseek", mode="track", dest_dir=download_dir,
                         artist=artist, title=title,
                         kwargs={"flac_only": flac_only,
                                 "timeout_seconds": timeout_seconds},
                         timeout_seconds=timeout_seconds)
    r.setdefault("files_attempted", len(r.get("paths") or []))
    return r
