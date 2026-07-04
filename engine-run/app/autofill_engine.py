"""Library autofill engine — Spotify → SpotiFLAC → Plex.

This runs as an APScheduler job alongside the existing watcher. For each
enabled source (Liked Songs, specific playlists, etc.) it iterates tracks,
groups them by (artist, album), checks whether each album is already known
to Lidarr (and therefore on the way to Plex), and POSTs the missing ones.

Key design choices:
- We don't move files ourselves. Lidarr handles search/download/import.
- One AutofillAction row per normalized (artist, album) so we don't re-attempt
  the same album every tick. All non-success statuses are throttled.
- Max 5 attempts before permanent 'abandoned' status.
- The autofill is opt-in PER SOURCE. The master toggle (autofill_enabled)
  short-circuits the whole loop.
"""
from __future__ import annotations

import json
import time
import logging
import os
import threading
import unicodedata
import re
from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import select, update

from . import auth_spotify, spotify_client
from .db import (
    AutofillAction, TrackMapping, AutoStar,
    get_config, session_scope, set_config,
    SessionLocal, LocalTrack,
)

log = logging.getLogger(__name__)

DEFAULT_SOURCES = ["liked"]


# Sentinel ID for the auto-created Plex playlist that mirrors Spotify Liked Songs.
# Liked Songs aren't a real Spotify playlist (no playlist_id), so we use this
# string as the PlaylistPair.spotify_playlist_id. Cannot collide with a real
# Spotify ID because Spotify IDs are 22-char base62 without underscores.
LIKED_SONGS_SENTINEL = "__LIKED_SONGS__"


def ensure_liked_songs_pair() -> None:
    """Create the sentinel Liked-Songs PlaylistPair if it doesn't exist AND
    'liked' is in the configured autofill sources. Idempotent."""
    from .db import PlaylistPair  # local import to avoid circular at module load
    sources_json = get_config("autofill_sources_json") or json.dumps(DEFAULT_SOURCES)
    try:
        sources = json.loads(sources_json)
    except Exception:
        sources = list(DEFAULT_SOURCES)
    if "liked" not in sources:
        log.debug("ensure_liked_songs_pair: 'liked' not in sources; skipping")
        return
    with session_scope() as s:
        existing = s.scalar(
            select(PlaylistPair).where(
                PlaylistPair.spotify_playlist_id == LIKED_SONGS_SENTINEL
            )
        )
        if existing:
            return
        s.add(PlaylistPair(
            name="Spotify Liked Songs",
            spotify_playlist_id=LIKED_SONGS_SENTINEL,
            plex_enabled=True,
            enabled=True,
        ))
        log.info("ensure_liked_songs_pair: created sentinel PlaylistPair")
RETRY_AFTER_HOURS = 24                # back-off for any non-success status
MAX_ATTEMPTS = 5                      # B4: hard cap before 'abandoned'
ABANDONED_STATUSES = {"failed", "lookup_empty", "lookup_low_confidence"}  # B3 — library_existing is NOT here (it's a success-equivalent)
TICK_LOCK = threading.Lock()

# SF5: Dashboard "Right now" card reads this. None when idle; dict when actively
# downloading. Updated by picker_tick before/after the spotiflac_acquire call.
# Module-level (not DB) because it's transient and only meaningful in-process.
import threading as _threading
_ACQUISITION_LOCK = _threading.Lock()
CURRENT_ACQUISITIONS: dict[int, dict] = {}


# ── Zombie acquisition fix ─────────────────────────────────────
# Entries can get stuck in CURRENT_ACQUISITIONS when picker_tick hangs in a
# post-acquire step (placement, cover download, mirror trigger, sweep). The
# dashboard "Right now" card then shows ghost downloads forever. These helpers
# evict zombies aggressively from BOTH ends:
#   - on every UI read (get_current_acquisitions)
#   - on a 60s scheduled watchdog tick
import collections as _collections_aq

_STALE_ACQ_HARD_TIMEOUT_S = 600     # 10 min — definitely zombie regardless of state
_STALE_ACQ_POSTACQ_TIMEOUT_S = 180  # 3 min — files arrived but post-processing hung

# Circuit breaker: if last 5 picker_tick attempts all failed at provider level,
# pause picker_tick for 15min so we don't grind against broken upstreams.
_PICKER_OUTCOMES = _collections_aq.deque(maxlen=10)
_PICKER_OUTCOMES_LOCK = threading.Lock()
_PICKER_COOLDOWN_UNTIL = None  # datetime or None


def _prune_stale_acquisitions() -> int:
    """Pop zombie entries from CURRENT_ACQUISITIONS. Returns count pruned.
    Called by both get_current_acquisitions() and the watchdog tick."""
    from datetime import datetime as _dt
    now = _dt.utcnow()
    pruned = []
    with _ACQUISITION_LOCK:
        for rid, e in list(CURRENT_ACQUISITIONS.items()):
            try:
                started = e.get("started_at")
                if not started:
                    continue
                t0 = _dt.fromisoformat(started.rstrip("Z"))
                elapsed = (now - t0).total_seconds()
                done = e.get("tracks_done", 0) or 0
                total = e.get("tracks_total", 0) or 0
                files_done = total > 0 and done >= total
                # Hard zombie: been there too long regardless
                if elapsed > _STALE_ACQ_HARD_TIMEOUT_S:
                    CURRENT_ACQUISITIONS.pop(rid, None)
                    pruned.append((rid, elapsed, "hard-timeout"))
                # Post-acquire zombie: files arrived but post-processing hung
                elif files_done and elapsed > _STALE_ACQ_POSTACQ_TIMEOUT_S:
                    CURRENT_ACQUISITIONS.pop(rid, None)
                    pruned.append((rid, elapsed, "post-acquire-hang"))
            except Exception:
                pass
    for rid, el, kind in pruned:
        log.warning("ZOMBIE: pruned row_id=%s elapsed=%.0fs reason=%s", rid, el, kind)
    return len(pruned)


def acquisition_watchdog_tick() -> dict:
    """Runs every 60s: prunes zombie acquisitions + resets DB rows stuck >15min."""
    from datetime import datetime as _dt2, timedelta as _td
    pruned_mem = _prune_stale_acquisitions()
    # Also reset DB rows stuck in 'downloading' for >15min back to 'queued'.
    cutoff = _dt2.utcnow() - _td(minutes=15)
    pruned_db = 0
    try:
        from .db import AutofillAction
        with session_scope() as s:
            stuck = list(s.query(AutofillAction).filter(
                AutofillAction.status == "downloading",
                AutofillAction.last_attempt_at < cutoff,
            ).all())
            for r in stuck:
                # Only reset if not currently in-flight
                with _ACQUISITION_LOCK:
                    in_flight = r.id in CURRENT_ACQUISITIONS
                if not in_flight:
                    r.status = "queued"
                    r.note = (r.note or "")[:500] + " | watchdog: reset from downloading"
                    pruned_db += 1
    except Exception:
        log.exception("acquisition_watchdog_tick: DB reset failed")
    if pruned_mem or pruned_db:
        log.info("acquisition_watchdog: pruned %d memory + %d DB rows", pruned_mem, pruned_db)
    # Smart ticker: re-tune the picker interval to recent download speed so the slots stay
    # full (reschedules only when it drifts >=3s; also keeps max_instances == slots).
    smart_interval = None
    try:
        from .main import scheduler
        from apscheduler.triggers.interval import IntervalTrigger
        job = scheduler.get_job("library_autofill_picker")
        if job is not None:
            # Auto-tune concurrency (one AIMD step/cycle) — no manual slots knob.
            slots = _adjust_smart_concurrency()
            smart_interval = compute_smart_picker_interval()
            if getattr(job, "max_instances", None) != slots:
                scheduler.modify_job("library_autofill_picker", max_instances=slots)
            cur = getattr(getattr(job, "trigger", None), "interval", None)
            cur_s = int(cur.total_seconds()) if cur else None
            if cur_s is None or abs(cur_s - smart_interval) >= 3:
                scheduler.reschedule_job("library_autofill_picker", trigger=IntervalTrigger(seconds=smart_interval))
                log.info("smart-ticker: picker interval %ss -> %ds (slots=%d)", cur_s, smart_interval, slots)
    except Exception:
        log.debug("smart-ticker: reschedule skipped", exc_info=True)
    return {"pruned_memory": pruned_mem, "pruned_db": pruned_db, "smart_interval": smart_interval}



# Run heavy media subprocesses (ffmpeg decode, etc.) at idle CPU + I/O priority so they
# never starve Plex playback/transcode of a high-bitrate movie. Computed once at import.
_LOWPRIO_PREFIX: list[str] = []
try:
    import shutil as _sh_lp
    if _sh_lp.which("nice"):
        _LOWPRIO_PREFIX += ["nice", "-n", "19"]
    if _sh_lp.which("ionice"):
        _LOWPRIO_PREFIX += ["ionice", "-c", "3"]
except Exception:
    _LOWPRIO_PREFIX = []


def _plex_active_video_sessions() -> int:
    """Count active Plex VIDEO sessions whose bitrate is at/above a threshold (default
    25 Mbps) so the music pipeline only yields to HIGH-bitrate playback. Low-bitrate
    streams don't stress the disk/network, so we keep downloading through them.
    Best-effort; returns 0 on any error (never blocks the picker on a Plex hiccup)."""
    try:
        from . import plex_client
        srv = plex_client._connect()
        if not srv:
            return 0
        try:
            min_kbps = float(get_config("autofill_pause_min_video_mbps", "25") or "25") * 1000.0
        except Exception:
            min_kbps = 25000.0
        count = 0
        for sess in (srv.sessions() or []):
            if getattr(sess, "type", "") not in ("movie", "episode", "clip"):
                continue
            br = 0
            try:
                for m in (getattr(sess, "media", None) or []):
                    b = getattr(m, "bitrate", None)
                    if b:
                        br = max(br, int(b))
            except Exception:
                br = 0
            if br >= min_kbps:
                count += 1
        return count
    except Exception:
        return 0


def _plex_rate(track_obj, rating: float) -> bool:
    """Set a Plex track's user rating (the Plexamp 'star'/like). Tries .rate() then .edit()."""
    try:
        track_obj.rate(rating)
        return True
    except Exception:
        pass
    try:
        track_obj.edit(**{"userRating.value": rating, "userRating.locked": 1})
        return True
    except Exception:
        log.debug("_plex_rate: both rate paths failed for %s", getattr(track_obj, "ratingKey", "?"))
        return False


def star_liked_songs_in_plex_tick(batch: int = 120) -> dict:
    """Star (rate) every Spotify-liked song that's in the Plex library so it shows as
    'liked' in Plexamp. Resumable + gentle: marks each like starred once done, and
    round-robins through not-yet-found likes (the track may not be downloaded yet)."""
    out = {"checked": 0, "starred": 0, "not_found": 0, "errors": 0}
    if (get_config("plex_star_liked_enabled", "1") or "1") != "1":
        out["skipped"] = "disabled"
        return out
    try:
        rating = float(get_config("plex_liked_star_rating", "10") or "10")
    except Exception:
        rating = 10.0
    from . import plex_client
    srv = plex_client._connect()
    if not srv:
        out["error"] = "plex unreachable"
        return out
    from .db import SessionLocal, SpotifyLikedTrack, TrackMapping
    with SessionLocal() as s:
        rows = list(s.scalars(
            select(SpotifyLikedTrack)
            .where(SpotifyLikedTrack.plex_starred_at.is_(None))
            .order_by(SpotifyLikedTrack.plex_star_checked_at.is_(None).desc(),
                      SpotifyLikedTrack.plex_star_checked_at.asc())
            .limit(max(1, batch))
        ).all())
        items = [(r.spotify_track_id, r.title, r.artist) for r in rows]
        tids = [it[0] for it in items]
        maps = {}
        if tids:
            for m in s.scalars(select(TrackMapping)
                               .where(TrackMapping.spotify_track_id.in_(tids))
                               .where(TrackMapping.plex_track_key.isnot(None))).all():
                maps[m.spotify_track_id] = m.plex_track_key
    starred_tids = set()
    for tid, title, artist in items:
        out["checked"] += 1
        track_obj = None
        pk = maps.get(tid)
        if pk:
            try:
                track_obj = srv.fetchItem(int(pk))
            except Exception:
                track_obj = None
        if track_obj is None:
            try:
                t = plex_client.search_track(title or "", artist or "")
                if t and getattr(t, "key", None):
                    cand = srv.fetchItem(int(t.key))
                    # Guard against fuzzy mis-matches: only accept the fallback hit if
                    # its title actually matches the liked song. Otherwise we'd star a
                    # different track (the source of stars on non-liked songs).
                    if cand is not None and _norm_ta(getattr(cand, "title", "")) == _norm_ta(title or ""):
                        track_obj = cand
            except Exception:
                track_obj = None
        if track_obj is None:
            out["not_found"] += 1
            continue
        if _plex_rate(track_obj, rating):
            out["starred"] += 1
            starred_tids.add(tid)
            if _autostar_enabled():
                _autostar_record_liked(track_obj, tid, title, artist)
        else:
            out["errors"] += 1
    if items:
        _now2 = datetime.utcnow()
        with SessionLocal() as s:
            for tid, _, _ in items:
                r = s.get(SpotifyLikedTrack, tid)
                if not r:
                    continue
                r.plex_star_checked_at = _now2
                if tid in starred_tids:
                    r.plex_starred_at = _now2
            s.commit()
    if out["checked"]:
        log.info("star_liked: checked=%d starred=%d not_found=%d errors=%d",
                 out["checked"], out["starred"], out["not_found"], out["errors"])
    return out


def _norm_ta(s):
    s = (s or "").lower().strip()
    s = re.sub(r"[\s\-_]+", " ", s)
    s = re.sub(r"[^\w\s]", "", s)
    return s.strip()


def _core_ta(s):
    """Normalized 'core' title for lenient matching: drops trailing parentheticals
    ('(Single Version)', '(feat. X)', '(Stripped)'), a trailing ' - <anything>'
    (e.g. '- The Chainsmokers, Daya', '- Remastered'), and trailing 'feat. ...' —
    the streaming-vs-library title decorations that otherwise cause false mismatches."""
    s = (s or "").strip()
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*$", "", s).strip()
    s = re.sub(r"\s+-\s+.*$", "", s).strip()
    s = re.sub(r"\s+feat\.?\s+.*$", "", s, flags=re.I).strip()
    return _norm_ta(s)


def _build_liked_lookup(s):
    """Return lookups for every Spotify-liked song, used to decide whether a starred
    Plex track is legitimately liked: (mapped_plex_keys, exact (title,artist) pairs,
    exact titles, core (title,artist) pairs, core titles)."""
    from .db import SpotifyLikedTrack, TrackMapping
    liked_rows = list(s.scalars(select(SpotifyLikedTrack)).all())
    pairs = {(_norm_ta(r.title), _norm_ta(r.artist)) for r in liked_rows}
    titles = {_norm_ta(r.title) for r in liked_rows}
    core_pairs = {(_core_ta(r.title), _norm_ta(r.artist)) for r in liked_rows}
    core_titles = {_core_ta(r.title) for r in liked_rows}
    keys = set()
    tids = [r.spotify_track_id for r in liked_rows]
    for i in range(0, len(tids), 500):
        chunk = tids[i:i + 500]
        if not chunk:
            continue
        for m in s.scalars(select(TrackMapping)
                           .where(TrackMapping.spotify_track_id.in_(chunk))
                           .where(TrackMapping.plex_track_key.isnot(None))).all():
            keys.add(str(m.plex_track_key))
    return keys, pairs, titles, core_pairs, core_titles


def _plex_unrate(track_obj) -> bool:
    """Clear a Plex track's user rating (remove the Plexamp star)."""
    try:
        track_obj.rate(0.0)
        return True
    except Exception:
        pass
    try:
        track_obj.edit(**{"userRating.value": 0, "userRating.locked": 0})
        return True
    except Exception:
        return False


def reconcile_liked_stars(apply: bool = False) -> dict:
    """Remove the Plexamp 'star' from any track rated at the liked-star value that is
    NOT actually a Spotify-liked song. Conservative: only touches tracks rated EXACTLY
    the configured liked rating, and KEEPS a star if the track matches a liked song by
    mapped key, (title, artist), or even title alone (errs toward keeping). When
    apply=True it writes a rollback manifest to /data so every removal is reversible."""
    out = {"rated": 0, "liked_ok": 0, "to_unstar": 0, "unstarred": 0,
           "errors": 0, "applied": apply, "manifest": None}
    try:
        rating = float(get_config("plex_liked_star_rating", "10") or "10")
    except Exception:
        rating = 10.0
    from . import plex_client
    srv = plex_client._connect()
    if not srv:
        out["error"] = "plex unreachable"
        return out
    section = plex_client._music_section(srv)
    if not section:
        out["error"] = "no music section"
        return out

    from .db import SessionLocal
    with SessionLocal() as s:
        liked_keys, liked_pairs, liked_titles, liked_core_pairs, liked_core_titles = _build_liked_lookup(s)
    # Never strip a star Plexify placed on purpose (auto-star feature owns these). Empty
    # set when the feature is off → reconcile behaves exactly as before.
    _autostar_managed = _autostar_keyset(filler_only=False)

    rated = []
    for flt in ({"track.userRating>>=": 1}, {"userRating>>=": 1}):
        try:
            rated = section.searchTracks(filters=flt, maxresults=200000)
            break
        except Exception:
            continue
    out["rated"] = len(rated)

    victims = []  # (track_obj, old_rating)
    for t in rated:
        ur = t.userRating or 0
        if abs(ur - rating) > 0.01:
            continue  # not our liked-star value — leave the user's other ratings alone
        key = str(getattr(t, "ratingKey", "") or "")
        if key in _autostar_managed:
            out["liked_ok"] += 1
            continue  # Plexify-managed auto-star — keep it
        ti = _norm_ta(getattr(t, "title", ""))
        cti = _core_ta(getattr(t, "title", ""))
        ar = _norm_ta(getattr(t, "grandparentTitle", "") or getattr(t, "originalTitle", ""))
        # Lenient on purpose — err toward KEEPING a star (a liked song may sit in Plex
        # under a version/feat/remaster title). Only flag when nothing matches by
        # mapped key, exact (title,artist), core (title,artist), or title alone.
        is_liked = (
            key in liked_keys
            or (ti, ar) in liked_pairs
            or (cti, ar) in liked_core_pairs
            or ti in liked_titles
            or cti in liked_core_titles
        )
        if is_liked:
            out["liked_ok"] += 1
        else:
            victims.append((t, ur))
    out["to_unstar"] = len(victims)

    if not apply:
        out["sample"] = [
            {"title": getattr(t, "title", "?"),
             "artist": getattr(t, "grandparentTitle", "?"),
             "ratingKey": str(getattr(t, "ratingKey", ""))}
            for t, _ in victims[:30]
        ]
        return out

    # APPLY — unstar + write rollback manifest.
    import json as _json, time as _time
    ts = _time.strftime("%Y%m%d_%H%M%S")
    manifest = {"ts": ts, "rating": rating, "removed": []}
    for t, old in victims:
        if _plex_unrate(t):
            out["unstarred"] += 1
            manifest["removed"].append(
                {"ratingKey": str(getattr(t, "ratingKey", "")),
                 "title": getattr(t, "title", ""),
                 "artist": getattr(t, "grandparentTitle", ""),
                 "old_rating": old})
        else:
            out["errors"] += 1
    path = f"/data/unstar_rollback_{ts}.json"
    try:
        with open(path, "w") as fh:
            _json.dump(manifest, fh, indent=2)
        out["manifest"] = path
    except Exception:
        log.exception("reconcile_liked_stars: could not write manifest")
    log.info("reconcile_liked_stars: %s", out)
    return out




_PLEX2SP_LOCK = threading.Lock()


def sync_plex_ratings_to_spotify_tick(batch: int = 80) -> dict:
    """Plex rating -> Spotify like. When you 5★ a song in Plex/Plexamp, add it to your
    Spotify Liked Songs. The reverse of the star-push, making likes bidirectional.

    Only pushes ratings that AREN'T already liked (so it ignores the songs the
    star-push put a 5★ on). Resolves the Spotify track id from the local download
    mapping first, then a Spotify search fallback. Needs the user-library-modify
    scope — reconnect Spotify once after the scope change for this to work."""
    out = {"rated": 0, "to_like": 0, "liked": 0, "unresolved": 0,
           "errors": 0, "needs_reconnect": False, "skipped": False}
    if (get_config("plex_ratings_to_spotify_enabled", "1") or "1") != "1":
        out["skipped"] = "disabled"
        return out
    if not _PLEX2SP_LOCK.acquire(blocking=False):
        out["skipped"] = "already running"
        return out
    try:
        try:
            min_rating = float(get_config("plex_to_spotify_min_rating", "10") or "10")
        except Exception:
            min_rating = 10.0
        from . import plex_client, spotify_client, auth_spotify
        srv = plex_client._connect()
        if not srv:
            out["error"] = "plex unreachable"
            return out
        section = plex_client._music_section(srv)
        if not section:
            out["error"] = "no music section"
            return out
        if not auth_spotify.get_client():
            out["error"] = "spotify not connected"
            return out

        from .db import SessionLocal, TrackMapping, SpotifyLikedTrack
        with SessionLocal() as s:
            (liked_keys, liked_pairs, liked_titles,
             liked_core_pairs, liked_core_titles) = _build_liked_lookup(s)
        # Plexify-auto-starred FILLER (album-completion tracks the user never liked) must
        # never be pushed to Spotify Liked — skip them before the live-search fallback can
        # resolve a homonym. Empty set when the feature is off → no behavior change.
        _autostar_filler = _autostar_keyset(filler_only=True)

        rated = []
        # Fetch ALL rated tracks (the '>>=' operator is quirky for an exact threshold),
        # then apply the min_rating cutoff in Python below — same approach as the unstar.
        for flt in ({"track.userRating>>=": 1}, {"userRating>>=": 1}):
            try:
                rated = section.searchTracks(filters=flt, maxresults=200000)
                break
            except Exception:
                continue
        out["rated"] = len(rated)

        # Candidates: rated >= threshold AND not already a known Spotify like.
        cands = []
        for t in rated:
            if (t.userRating or 0) < min_rating - 0.01:
                continue
            key = str(getattr(t, "ratingKey", "") or "")
            if key in _autostar_filler:
                out["unresolved"] += 1
                continue  # Plexify filler — never leaks into Spotify Liked
            ti = _norm_ta(getattr(t, "title", ""))
            cti = _core_ta(getattr(t, "title", ""))
            ar = _norm_ta(getattr(t, "grandparentTitle", "") or getattr(t, "originalTitle", ""))
            already = (key in liked_keys or (ti, ar) in liked_pairs
                       or (cti, ar) in liked_core_pairs or ti in liked_titles
                       or cti in liked_core_titles)
            if not already:
                cands.append(t)
        out["to_like"] = len(cands)
        cands = cands[:max(1, batch)]
        if not cands:
            log.info("plex->spotify: %s", out)
            return out

        # Resolve Spotify ids: local download mapping first (exact), search fallback.
        keys = [str(getattr(t, "ratingKey", "")) for t in cands]
        rev = {}
        with SessionLocal() as s:
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                if not chunk:
                    continue
                for m in s.scalars(select(TrackMapping)
                                   .where(TrackMapping.plex_track_key.in_(chunk))
                                   .where(TrackMapping.spotify_track_id.isnot(None))).all():
                    rev[str(m.plex_track_key)] = m.spotify_track_id

        to_save = []  # (sid, title, artist, album)
        _rl_streak = 0  # consecutive rate-limited searches this run
        for t in cands:
            key = str(getattr(t, "ratingKey", ""))
            title = getattr(t, "title", "") or ""
            artist = getattr(t, "grandparentTitle", "") or getattr(t, "originalTitle", "") or ""
            album = getattr(t, "parentTitle", "") or ""
            sid = rev.get(key)
            if not sid:
                # MISS-CACHE: the same ~20 unresolved titles were re-searched on live
                # Spotify every 15-min run, forever (they will never suddenly resolve).
                # Remember misses for 24h. Keyed by the SEARCH TEXT, not ratingKey —
                # duplicate Plex items of the same song must share one cache entry
                # instead of each burning a live search.
                global _RATINGS_SEARCH_NEG
                try:
                    _RATINGS_SEARCH_NEG
                except NameError:
                    _RATINGS_SEARCH_NEG = {}
                _negkey = ((title or "").strip().lower(), (artist or "").strip().lower())
                _neg = _RATINGS_SEARCH_NEG.get(_negkey)
                if _neg and (time.time() - _neg) < 24 * 3600:
                    out["unresolved"] += 1
                    continue
                try:
                    tr = spotify_client.search_track(title, artist)
                    sid = getattr(tr, "id", None) if tr else None
                except Exception:
                    sid = None
                if spotify_client.was_search_ratelimited():
                    # 429 is not a miss: don't poison the 24h cache, and stop the
                    # search phase after two strikes — each rate-limited search
                    # burns ~30s of retries, and a batch of 80 was hammering the
                    # API for an hour+ inside a single 15-minute tick.
                    _rl_streak += 1
                    out["rate_limited"] = _rl_streak
                    if _rl_streak >= 2:
                        log.warning("plex->spotify: Spotify rate-limited — aborting search phase this run")
                        break
                    out["unresolved"] += 1
                    continue
                _rl_streak = 0
                if not sid:
                    if len(_RATINGS_SEARCH_NEG) > 10000:   # prune expired (>24h), then hard-bound
                        _cut = time.time() - 24 * 3600
                        for _k in [k for k, v in list(_RATINGS_SEARCH_NEG.items()) if v < _cut]:
                            _RATINGS_SEARCH_NEG.pop(_k, None)
                        if len(_RATINGS_SEARCH_NEG) > 10000:
                            _RATINGS_SEARCH_NEG.clear()
                    _RATINGS_SEARCH_NEG[_negkey] = time.time()
            if sid:
                to_save.append((sid, title, artist, album))
            else:
                out["unresolved"] += 1

        if not to_save:
            log.info("plex->spotify: %s", out)
            return out

        res = spotify_client.save_tracks([sid for sid, _, _, _ in to_save])
        saved_set = set(res.get("saved", []))
        out["liked"] = len(saved_set)
        out["needs_reconnect"] = bool(res.get("needs_scope"))
        if len(saved_set) < len(to_save) and not out["needs_reconnect"]:
            out["errors"] = len(to_save) - len(saved_set)

        # Record newly-liked tracks locally so we recognize them as liked going
        # forward (won't re-push, and the star-push/reconcile see them as liked).
        if saved_set:
            from datetime import datetime as _dt
            with SessionLocal() as s:
                for sid, title, artist, album in to_save:
                    if sid not in saved_set:
                        continue
                    if s.get(SpotifyLikedTrack, sid):
                        continue
                    s.add(SpotifyLikedTrack(spotify_track_id=sid, title=title,
                                            artist=artist, album=album,
                                            added_at_spotify=_dt.utcnow()))
                s.commit()
    except Exception:
        log.exception("sync_plex_ratings_to_spotify_tick: unexpected failure")
    finally:
        try:
            _PLEX2SP_LOCK.release()
        except Exception:
            pass
    log.info("plex->spotify: %s", out)
    return out


# =====================================================================================
# AUTO-STAR  —  Plexify auto-★s (5-star) every track it places in Plex; UN-starring one
# signals "this file is wrong" → fire the existing dispute pipeline (attic + blacklist the
# source + re-acquire the correct copy). Gated by `autostar_manage_enabled` (default off)
# and an `autostar_dry_run` flag. All of this is a NEW trigger onto existing machinery
# (dispute_song/dispute_file, _plex_rate, TrackMapping, the recently-added-albums scan).
# =====================================================================================

def _autostar_enabled() -> bool:
    return (get_config("autostar_manage_enabled", "0") or "0") == "1"


def _autostar_dry_run() -> bool:
    return (get_config("autostar_dry_run", "0") or "0") == "1"


def _autostar_norm_key(key) -> str:
    """Bare numeric ratingKey string from '/library/metadata/123', '123', or 123."""
    return str(key or "").rsplit("/", 1)[-1].strip()


def _autostar_localize(path: str) -> str:
    """Plex-server-view path → this Mac's local mount path (idempotent if already local)."""
    if not path:
        return path or ""
    pp = _plex_prefix()
    if pp and path.startswith(pp):
        return _LOCAL_PATH_PREFIX + path[len(pp):]
    if path.startswith("/plexify-music/"):
        return _LOCAL_PATH_PREFIX + path[len("/plexify-music"):]
    return path


def _autostar_keyset(filler_only: bool = False) -> set:
    """Bare ratingKeys Plexify auto-starred (all, or filler-only = no spotify_track_id).
    Read by the two rating-sync patches. Empty when the feature is OFF → zero behavior change."""
    if not _autostar_enabled():
        return set()
    try:
        with SessionLocal() as s:
            q = select(AutoStar.plex_track_key)
            if filler_only:
                q = q.where(AutoStar.spotify_track_id.is_(None))
            return {str(k) for (k,) in s.execute(q).all()}
    except Exception:
        log.exception("_autostar_keyset failed")
        return set()


def _autostar_bump_miss(aid, now):
    try:
        with SessionLocal() as s:
            r = s.get(AutoStar, aid)
            if r:
                r.miss_count = (r.miss_count or 0) + 1
                if not r.first_missing_at:
                    r.first_missing_at = now
                s.commit()
    except Exception:
        log.exception("autostar bump_miss")


def _autostar_reset_miss(aid):
    try:
        with SessionLocal() as s:
            r = s.get(AutoStar, aid)
            if r:
                r.miss_count = 0
                r.first_missing_at = None
                r.last_seen_starred_at = datetime.utcnow()
                s.commit()
    except Exception:
        log.exception("autostar reset_miss")


def _autostar_delete(aid):
    try:
        with SessionLocal() as s:
            r = s.get(AutoStar, aid)
            if r:
                s.delete(r)
                s.commit()
    except Exception:
        log.exception("autostar delete")


def _autostar_handle_gone(aid, file_path):
    """A managed track's ratingKey vanished (deleted / hygiene-hidden / rescan re-key).
    NOT an un-star. If the file is gone from disk too, drop the row; if it still exists,
    keep the row (a later place-tick re-keys it). Never dispute on a vanished key."""
    try:
        local = _autostar_localize(file_path or "")
        if not local or not os.path.isfile(local):
            _autostar_delete(aid)
    except Exception:
        log.exception("autostar handle_gone")


def _autostar_audit(rec: dict):
    try:
        rec = dict(rec)
        rec["ts"] = datetime.utcnow().isoformat() + "Z"
        with open("/data/autostar_disputes.jsonl", "a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass


def _autostar_record_liked(track_obj, tid, title, artist):
    """Upsert an AutoStar row when the liked-song star-push stars a track — so liked songs
    starred outside the recently-added scan are still un-star-watchable. Fill-only."""
    try:
        rk = _autostar_norm_key(getattr(track_obj, "ratingKey", ""))
        if not rk:
            return
        loc = (getattr(track_obj, "locations", None) or [None])[0]
        lpath = _autostar_localize(loc) if loc else None
        with SessionLocal() as s:
            row = s.scalar(select(AutoStar).where(AutoStar.plex_track_key == rk))
            if row is None:
                row = AutoStar(plex_track_key=rk, starred_at=datetime.utcnow())
                s.add(row)
            if tid and not row.spotify_track_id:
                row.spotify_track_id = tid
            if lpath and not row.file_path:
                row.file_path = lpath
            row.title = row.title or title
            row.artist = row.artist or artist
            row.last_seen_starred_at = datetime.utcnow()
            row.miss_count = 0
            row.first_missing_at = None
            s.commit()
    except Exception:
        log.exception("_autostar_record_liked")


def autostar_place_tick(batch: int = 200) -> dict:
    """Star (5★) every track Plexify has recently placed in Plex — liked songs AND
    album-completion filler — and record each in AutoStar so an un-star is detectable.
    Rides the recently-added-albums scan (same source plex_import_verify_tick uses).
    Idempotent: tracks already recorded are skipped, so steady state is cheap."""
    out = {"albums": 0, "starred": 0, "recorded": 0, "skipped": 0, "errors": 0}
    if not _autostar_enabled():
        out["skipped"] = "disabled"
        return out
    try:
        rating = float(get_config("plex_liked_star_rating", "10") or "10")
    except Exception:
        rating = 10.0
    from . import plex_client
    from .db import SpotifyLikedTrack
    srv = plex_client._connect()
    if not srv:
        out["error"] = "plex unreachable"
        return out
    sec = plex_client._music_section(srv)
    if not sec:
        out["error"] = "no music section"
        return out
    try:
        recent = sec.recentlyAdded(libtype="album", maxresults=max(10, batch))
    except Exception:
        log.exception("autostar_place: recentlyAdded failed")
        out["error"] = "recentlyAdded failed"
        return out

    # Build once: keys we've already recorded, liked-title lookups, and path→row index.
    with SessionLocal() as s:
        known = {str(k) for (k,) in s.execute(select(AutoStar.plex_track_key)).all()}
        liked_rows = list(s.scalars(select(SpotifyLikedTrack)).all())
        liked_title_by_tid = {lr.spotify_track_id: _norm_title_key(lr.title or "") for lr in liked_rows}
        # Keyed on (artist, title). A title-only key mis-attributes an album-filler track to
        # an unrelated liked song that shares a common title (e.g. 'Intro'/'Interlude'/'Outro'),
        # which would later dispute the WRONG song when that filler is un-starred.
        liked_by_artist_title = {}
        for lr in liked_rows:
            liked_by_artist_title.setdefault(
                (_norm_title_key(lr.artist or ""), _norm_title_key(lr.title or "")),
                lr.spotify_track_id)
        path_to_row, row_tids = {}, {}
        for r in s.scalars(select(AutofillAction).where(AutofillAction.imported_paths.isnot(None))).all():
            try:
                paths = json.loads(r.imported_paths or "[]")
            except Exception:
                paths = []
            for p in paths:
                path_to_row[_autostar_localize(p)] = r.id
            try:
                row_tids[r.id] = set(json.loads(r.track_ids_json or "[]"))
            except Exception:
                row_tids[r.id] = set()

    done = 0
    for al in recent:
        if done >= batch:
            break
        out["albums"] += 1
        try:
            trs = al.tracks()
        except Exception:
            continue
        for t in trs:
            if done >= batch:
                break
            rk = _autostar_norm_key(getattr(t, "ratingKey", ""))
            if not rk or rk in known:
                out["skipped"] += 1
                continue
            loc = (getattr(t, "locations", None) or [None])[0]
            lpath = _autostar_localize(loc) if loc else None
            row_id = path_to_row.get(lpath) if lpath else None
            title = getattr(t, "title", "") or ""
            tk = _norm_title_key(title)
            # liked vs filler: prefer a liked track in THIS row whose title matches; else global.
            sid = None
            if row_id is not None:
                sid = next((tid for tid in row_tids.get(row_id, ()) if liked_title_by_tid.get(tid) == tk), None)
            if not sid:
                t_artist = getattr(t, "originalTitle", None) or getattr(t, "grandparentTitle", "") or ""
                sid = liked_by_artist_title.get((_norm_title_key(t_artist), tk))
            if _plex_rate(t, rating):
                out["starred"] += 1
            else:
                out["errors"] += 1
                continue
            try:
                with SessionLocal() as s:
                    row = s.scalar(select(AutoStar).where(AutoStar.plex_track_key == rk))
                    if row is None:
                        row = AutoStar(plex_track_key=rk, starred_at=datetime.utcnow())
                        s.add(row)
                        out["recorded"] += 1
                    if row_id is not None and row.row_id is None:
                        row.row_id = row_id
                    if sid and not row.spotify_track_id:
                        row.spotify_track_id = sid
                    if lpath and not row.file_path:
                        row.file_path = lpath
                    row.artist = getattr(t, "grandparentTitle", None) or row.artist
                    row.album = getattr(t, "parentTitle", None) or row.album
                    row.title = title or row.title
                    row.last_seen_starred_at = datetime.utcnow()
                    row.miss_count = 0
                    row.first_missing_at = None
                    s.commit()
                known.add(rk)
                done += 1
            except Exception:
                log.exception("autostar_place: upsert failed for key=%s", rk)
                out["errors"] += 1
    log.info("autostar_place: %s", out)
    return out


def autostar_unstar_detect_tick(batch: int = 500) -> dict:
    """Detect Plexify-starred tracks the user UN-starred, and — after a debounce — fire
    the dispute pipeline (attic + blacklist source + re-acquire). PARANOID: a wrong dispute
    attics a good file and re-downloads, so every dispute is gated by: track still exists in
    Plex, rating definitively below 5★, seen un-starred on 2 passes ≥ grace apart, file still
    on disk, and the pass wasn't a mass-anomaly."""
    from datetime import timedelta as _td
    out = {"managed": 0, "starred_now": 0, "suspects": 0, "confirmed": 0,
           "disputed": 0, "gone": 0, "resets": 0, "transient": False,
           "errors": 0, "dry_run": _autostar_dry_run()}
    if not _autostar_enabled():
        out["skipped"] = "disabled"
        return out
    try:
        rating = float(get_config("plex_liked_star_rating", "10") or "10")
    except Exception:
        rating = 10.0
    try:
        grace_min = int(get_config("autostar_dispute_grace_minutes", "15") or "15")
    except Exception:
        grace_min = 15
    try:
        cap = int(get_config("autostar_dispute_max_per_tick", "10") or "10")
    except Exception:
        cap = 10
    from . import plex_client
    srv = plex_client._connect()
    if not srv:
        out["error"] = "plex unreachable"
        return out
    sec = plex_client._music_section(srv)
    if not sec:
        out["error"] = "no music section"
        return out

    rated = []
    for flt in ({"track.userRating>>=": 1}, {"userRating>>=": 1}):
        try:
            rated = sec.searchTracks(filters=flt, maxresults=200000)
            break
        except Exception:
            continue
    starred_now = {_autostar_norm_key(getattr(t, "ratingKey", ""))
                   for t in rated if abs((t.userRating or 0) - rating) <= 0.01}
    out["starred_now"] = len(starred_now)

    with SessionLocal() as s:
        managed_snap = [(m.id, m.plex_track_key, m.row_id, m.spotify_track_id, m.file_path,
                         m.miss_count or 0, m.first_missing_at)
                        for m in s.scalars(select(AutoStar)).all()]
    out["managed"] = len(managed_snap)

    # Transient circuit-breaker: implausible collapse of the 5★ set (Plex reindex/outage).
    if managed_snap and len(starred_now) == 0:
        out["transient"] = True
        log.warning("autostar_detect: 0 starred but %d managed — transient, skipping pass", len(managed_snap))
        return out

    # Reset debounce for any managed track that's currently starred (handles off→on toggle).
    for m in managed_snap:
        if m[1] in starred_now and (m[5] > 0 or m[6] is not None):
            _autostar_reset_miss(m[0])
            out["resets"] += 1

    suspects = [m for m in managed_snap if m[1] not in starred_now]
    out["suspects"] = len(suspects)

    now = datetime.utcnow()
    disputed = 0
    for (aid, key, row_id, sid, file_path, miss_count, first_missing_at) in suspects:
        if disputed >= cap:
            break
        # G1: definitive existence + rating re-read.
        try:
            obj = srv.fetchItem(int(key))
        except Exception:
            obj = None
        if obj is None:
            out["gone"] += 1
            _autostar_handle_gone(aid, file_path)
            continue
        try:
            ur = obj.userRating or 0
        except Exception:
            ur = 0
        if ur >= rating - 0.01:
            _autostar_reset_miss(aid)   # bulk read was stale; still starred
            continue
        # G2: debounce — first sighting just records; dispute only after grace elapsed.
        if not first_missing_at:
            _autostar_bump_miss(aid, now)
            continue
        if (now - first_missing_at) < _td(minutes=grace_min):
            _autostar_bump_miss(aid, now)
            continue
        out["confirmed"] += 1
        if out["dry_run"]:
            _autostar_audit({"dry_run": True, "key": key, "row_id": row_id,
                             "spotify_track_id": sid, "file_path": file_path})
            _autostar_bump_miss(aid, now)   # keep observing under dry-run
            continue
        # Dispatch the dispute.
        res = None
        try:
            if sid:
                res = dispute_song(sid)
            elif row_id and file_path:
                local = _autostar_localize(file_path)
                if not os.path.isfile(local):        # G4: file gone → not a dispute
                    out["gone"] += 1
                    _autostar_handle_gone(aid, file_path)
                    continue
                res = dispute_file(row_id, local)
            else:
                _autostar_delete(aid)                # no dispatch coordinates
                continue
        except Exception:
            log.exception("autostar_detect: dispute failed for key=%s", key)
            out["errors"] += 1
            continue
        _autostar_audit({"key": key, "row_id": row_id, "spotify_track_id": sid,
                         "file_path": file_path, "result": res})
        if res and res.get("ok"):
            out["disputed"] += 1
            disputed += 1
            _autostar_delete(aid)   # re-acquire → re-place → place-tick re-stars → fresh row
        else:
            out["errors"] += 1
            _autostar_bump_miss(aid, now)
    log.info("autostar_detect: %s", out)
    return out


def _verify_flac_integrity(paths: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Decode each FLAC with ffmpeg to verify it's not truncated/corrupted.

    Returns (intact_paths, [(bad_path, error), ...]).
    Files that produce ANY ffmpeg stderr output are treated as bad — ffmpeg in
    `-v error` mode only writes when it hits decode errors / truncation.

    Runs serially (not parallelized) because there are usually 1-15 files per
    album and we want to keep this cheap when there's only 1 file.
    """
    import subprocess as _sp_int, os as _os_int
    intact: list[str] = []
    bad: list[tuple[str, str]] = []
    for p in paths or []:
        if not p or not _os_int.path.isfile(p):
            bad.append((p, "file missing"))
            continue
        try:
            with open(p, "rb") as _fh:
                if _fh.read(4) != b"fLaC":
                    bad.append((p, "not FLAC format (lossy/mislabeled .flac)"))
                    continue
        except OSError as _me:
            bad.append((p, "unreadable: %s" % _me))
            continue
        try:
            r = _sp_int.run(
                _LOWPRIO_PREFIX + ["ffmpeg", "-v", "error", "-i", p, "-f", "null", "-"],
                capture_output=True, text=True, timeout=60,
            )
            err = (r.stderr or "").strip()
            if r.returncode != 0 or err:
                bad.append((p, (err or f"exit={r.returncode}")[:200]))
            else:
                intact.append(p)
        except _sp_int.TimeoutExpired:
            bad.append((p, "ffmpeg timeout (>60s)"))
        except Exception as e:
            bad.append((p, str(e)[:200]))
    return intact, bad


def _is_provider_outage(error_str: str | None) -> bool:
    """Return True if the SpotiFLAC error signals a GLOBAL provider outage
    (not a per-album miss). In that case the fallback chain is futile — we
    should fail fast and move to the next album.

    Patterns seen in real logs:
      - "[qobuz] UNAVAILABLE: All 12 Qobuz stream APIs failed."
      - "[tidal] UNAVAILABLE: All 15 Tidal APIs failed (of 15 total, 0 in cooldown)."
      - "[amazon] UNAVAILABLE: spotbye2 API returned 503: Service unavailable"
      - "All providers failed after 1 attempt(s)"
      - "spotiflac timed out after 90s"

    If TWO OR MORE provider names appear with UNAVAILABLE, this is global.
    A single provider being down is normal — only declare outage if multiple are.
    """
    if not error_str:
        return False
    e = error_str.lower()
    if "all providers failed" in e:
        return True
    # Count distinct providers reporting UNAVAILABLE
    provider_names = ("qobuz", "tidal", "deezer", "amazon")
    down_count = sum(1 for p in provider_names if (p in e and "unavailable" in e))
    if down_count >= 2:
        return True
    # SpotiFLAC's own timeout signal
    if "spotiflac timed out" in e:
        return True
    return False


def _record_picker_outcome(outcome: str) -> None:
    """Track picker_tick outcomes for circuit-breaker decisions.
    outcome ∈ {'success', 'provider_failure', 'no_candidates', 'skipped'}"""
    from datetime import datetime as _dt3, timedelta as _td3
    global _PICKER_COOLDOWN_UNTIL
    with _PICKER_OUTCOMES_LOCK:
        _PICKER_OUTCOMES.append(outcome)
        recent = list(_PICKER_OUTCOMES)[-7:]  # match trip threshold
        # Trigger cooldown only on 5 consecutive PROVIDER failures (not no_candidates).
        if len(recent) >= 7 and all(o == "provider_failure" for o in recent):  # was 3 — relaxed  # was 5
            _PICKER_COOLDOWN_UNTIL = _dt3.utcnow() + _td3(minutes=5)  # was 15min
            log.warning("CIRCUIT BREAKER: pausing picker for 5min (7 consecutive provider failures)")
            _PICKER_OUTCOMES.clear()  # reset so we don't immediately re-trigger



# ─────────────────────────────────────────────────────────────────
# Adaptive SpotiFLAC auto-update health check.
# Module-level rolling counters of per-source successes over a 1h window.
# When SpotiFLAC has 0 successes in the last hour, the scheduled health-check
# tick (every 30min) probes GitHub for a newer commit and triggers an update.
# ─────────────────────────────────────────────────────────────────
import time as _time_provider
_RECENT_SUCCESSES = {"spotiflac": [], "soulseek": []}  # source -> [unix-ts]
_RECENT_SUCCESSES_LOCK = threading.Lock()
_SUCCESS_WINDOW_S = 3600  # 1 hour

# ── Smart picker ticker: the tick interval adapts to recent download speed so the `slots`
#    (max_instances) download lanes stay full — launch a new download roughly every
#    (median_recent_duration / slots) seconds. ──────────────────────────────────────────
_RECENT_ACQUIRE_SECONDS: list = []
_RECENT_ACQUIRE_LOCK = threading.Lock()
_ACQUIRE_WINDOW = 25

def _record_acquire_seconds(secs) -> None:
    try:
        secs = float(secs)
    except Exception:
        return
    if secs <= 0 or secs > 1800:
        return
    with _RECENT_ACQUIRE_LOCK:
        _RECENT_ACQUIRE_SECONDS.append(secs)
        if len(_RECENT_ACQUIRE_SECONDS) > _ACQUIRE_WINDOW:
            del _RECENT_ACQUIRE_SECONDS[0:len(_RECENT_ACQUIRE_SECONDS) - _ACQUIRE_WINDOW]


def compute_smart_picker_interval() -> int:
    """Picker tick interval (seconds) that keeps all `slots` lanes full given recent
    download speed: launch a new download every ~(median_duration / slots) s, clamped to
    [5, 30]. max_instances caps real concurrency, so a short interval just refills freed
    slots fast, while a long one (slow downloads) avoids wasted skipped ticks."""
    slots = _smart_concurrency()
    with _RECENT_ACQUIRE_LOCK:
        data = sorted(_RECENT_ACQUIRE_SECONDS)
    median = data[len(data) // 2] if data else 60.0
    return int(max(5, min(30, round(median / slots))))


def _record_provider_success(source: str) -> None:
    """Record a successful acquisition for adaptive-update decisions.
    source ∈ {'spotiflac', 'soulseek'}"""
    now = _time_provider.time()
    with _RECENT_SUCCESSES_LOCK:
        lst = _RECENT_SUCCESSES.setdefault(source, [])
        _RECENT_SUCCESSES[source] = [t for t in lst if (now - t) < _SUCCESS_WINDOW_S]
        _RECENT_SUCCESSES[source].append(now)


def get_provider_success_counts_1h() -> dict:
    """Returns counts of successes per source in the last hour."""
    now = _time_provider.time()
    out = {}
    with _RECENT_SUCCESSES_LOCK:
        for src, lst in _RECENT_SUCCESSES.items():
            out[src] = sum(1 for t in lst if (now - t) < _SUCCESS_WINDOW_S)
    return out


# ─────────────────────────────────────────────────────────────────
# Source attribution (dashboard "where did this come from?" tags).
# ─────────────────────────────────────────────────────────────────
_SPOTIFLAC_VERSION_CACHE = {"v": None, "ts": 0.0}

def get_spotiflac_version() -> str:
    """Human-readable SpotiFLAC version string: pip version + short commit.
    Cached for 1h — the underlying pip/direct_url probe is too slow for the
    picker hot path."""
    now = _time_provider.time()
    cached = _SPOTIFLAC_VERSION_CACHE
    if cached["v"] is not None and (now - cached["ts"]) < 3600:
        return cached["v"]
    ver = None
    try:
        import importlib.metadata as _ilm
        ver = _ilm.version("SpotiFLAC")
    except Exception:
        ver = None
    commit = None
    try:
        commit = _get_installed_spotiflac_commit()
    except Exception:
        commit = None
    parts = []
    if ver:
        parts.append("v" + str(ver))
    if commit:
        parts.append(str(commit)[:7])
    out = " ".join(parts) if parts else "unknown"
    cached["v"] = out
    cached["ts"] = now
    return out


def derive_source(provider) -> tuple:
    """Map an AcquireResult.provider string to (source, source_detail).

    source        ∈ {'soulseek', 'spotiflac'} — canonical, drives the chip class.
    source_detail — human-readable: the SpotiFLAC mirror + module version, or the
                    Soulseek peer name."""
    p = (provider or "").strip()
    low = p.lower()
    if low.startswith("soulseek"):
        peer = p.split(":", 1)[1].strip() if ":" in p else ""
        if peer and peer != "?":
            return "soulseek", "peer " + peer
        return "soulseek", "Soulseek peer"
    if low.startswith("telegram"):
        bot = p.split(":", 1)[1].strip() if ":" in p else "@BeatSpotBot"
        return "telegram", "Telegram " + (bot or "@BeatSpotBot")
    if low.startswith("squid"):
        return "squid", "squid.wtf · Qobuz hi-res"
    # Everything else is a SpotiFLAC mirror (qobuz / tidal / deezer / amazon / soulseek-primary handled above)
    mirror = p if p else "unknown mirror"
    return "spotiflac", f"{mirror} · SpotiFLAC {get_spotiflac_version()}"


# ─────────────────────────────────────────────────────────────────
# Recent terminal outcomes — short-lived (success/fail) ledger that the
# dashboard's "Right now" card polls to drive departure animations.
# A row vanishes from CURRENT_ACQUISITIONS the instant the acquire chain
# ends (well before the DB row is fully persisted), so the live UI needs
# this side-channel to know whether the disappeared album succeeded
# (green → slides into "Recently added") or failed (red → slides left + fades).
# ─────────────────────────────────────────────────────────────────
_RECENT_OUTCOMES = {}  # row_id -> {row_id, outcome, artist, album, source, source_detail, ts}
_RECENT_OUTCOMES_LOCK = threading.Lock()
_OUTCOMES_TTL_S = 90

def _record_outcome(row_id, outcome, artist, album, source=None, source_detail=None) -> None:
    """outcome ∈ {'success', 'fail'}."""
    try:
        rid = int(row_id)
    except Exception:
        return
    now = _time_provider.time()
    with _RECENT_OUTCOMES_LOCK:
        # prune expired
        for k in [k for k, v in list(_RECENT_OUTCOMES.items()) if (now - v.get("ts", 0)) > _OUTCOMES_TTL_S]:
            _RECENT_OUTCOMES.pop(k, None)
        _RECENT_OUTCOMES[rid] = {
            "row_id": rid,
            "outcome": outcome,
            "artist": artist or "",
            "album": album or "",
            "source": source,
            "source_detail": source_detail,
            "ts": now,
        }


def _quality_rank(q) -> int:
    """Rank a FLAC quality string so we can tell an upgrade from a sidegrade.
    hi-res > CD/lossless > anything > unknown."""
    q = (q or "").upper()
    if "HI_RES" in q or "HIRES" in q:
        return 3
    if "LOSSLESS" in q:
        return 2
    if q:
        return 1
    return 0


def _actual_flac_quality(paths) -> str:
    """Read the ACTUAL delivered quality from the files on disk (never the requested
    target). Hi-Res = 24-bit (any rate) OR 16-bit > 48kHz — same rule as the dashboard
    feed. Returns 'HI_RES_LOSSLESS' | 'LOSSLESS' | 'LOSSY' | '' (unknown)."""
    best = ""
    for p in (paths or []):
        try:
            ext = os.path.splitext(p)[1].lower()
            if ext == ".flac":
                from mutagen.flac import FLAC
                i = FLAC(p).info
                bits = int(getattr(i, "bits_per_sample", 0) or 0)
                rate = int(getattr(i, "sample_rate", 0) or 0)
                if bits >= 24 or (bits and rate > 48000):
                    return "HI_RES_LOSSLESS"      # hi-res — best, stop early
                best = "LOSSLESS"
            elif ext in (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".alac"):
                if not best:
                    best = "LOSSY"
        except Exception:
            pass
    return best


# ── Global song-level dedup ledger ──────────────────────────────────────────
# Make it impossible to download the same SONG twice unless it's a genuine quality
# upgrade. Keyed by ISRC (exact recording) AND by normalized artist+title (same song
# across different albums/compilations). Every acquisition path that keeps a file
# consults this first; every successful import teaches it. Kill-switch: set config
# autofill_dedup_tracks=0 (no rebuild needed) to disable instantly.

_ISRC_FILE_RE = re.compile(r'^[A-Z]{2}[A-Z0-9]{3}\d{7}$', re.I)


def _dedup_enabled() -> bool:
    return (get_config("autofill_dedup_tracks", "1") or "1") == "1"


def _norm_isrc(isrc) -> str:
    return re.sub(r"[^A-Z0-9]", "", (isrc or "").upper())


def _track_key(artist, title) -> str:
    """Normalized 'artist\\x1ftitle' — the same-song key. Empty when there's no usable title."""
    t = _core_ta(title or "")
    if not t:
        return ""
    return _norm_ta(artist or "") + "\x1f" + t


def _file_song_info(path):
    """(artist, title, isrc, quality_rank) from an audio file's tags + stream info.
    Best-effort, single file open. Returns ('','','',0) when unreadable."""
    artist = title = isrc = ""
    rank = 0
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext == ".flac":
            from mutagen.flac import FLAC
            f = FLAC(path)
            info = f.info
            bits = int(getattr(info, "bits_per_sample", 0) or 0)
            rate = int(getattr(info, "sample_rate", 0) or 0)
            rank = 3 if (bits >= 24 or (bits and rate > 48000)) else 2
        else:
            from mutagen import File as _MF
            f = _MF(path)
            if f is None:
                return ("", "", "", 0)
            rank = 1

        def _g(*keys):
            for k in keys:
                try:
                    v = f.get(k)
                except Exception:
                    v = None
                if v:
                    return str(v[0] if isinstance(v, (list, tuple)) else v)
            return ""
        artist = _g("artist", "ARTIST", "albumartist", "\xa9ART", "TPE1")
        title = _g("title", "TITLE", "\xa9nam", "TIT2")
        isrc = _g("isrc", "ISRC", "TSRC")
    except Exception:
        return ("", "", "", 0)
    return (artist, title, _norm_isrc(isrc), rank)


def _ledger_have_quality(s, artist, title, isrc):
    """Best quality_rank we already have for this song (match by ISRC or by artist+title),
    or None if it's never been acquired."""
    from .db import AcquiredTrack
    best = None
    iso = _norm_isrc(isrc)
    if iso:
        for r in s.query(AcquiredTrack.quality_rank).filter(AcquiredTrack.isrc == iso).all():
            best = max(best if best is not None else -1, int(r[0] or 0))
    tk = _track_key(artist, title)
    if tk:
        for r in s.query(AcquiredTrack.quality_rank).filter(AcquiredTrack.tkey == tk).all():
            best = max(best if best is not None else -1, int(r[0] or 0))
    return best


def _ledger_remember(s, artist, title, isrc, quality_rank, path=None, quality=None) -> bool:
    """Record (or quality-bump) a song in the global ledger. Returns True if stored."""
    from .db import AcquiredTrack
    iso = _norm_isrc(isrc) or None
    tk = _track_key(artist, title)
    if not tk and not iso:
        return False
    row = None
    if iso:
        row = s.query(AcquiredTrack).filter(AcquiredTrack.isrc == iso).first()
    if row is None and tk:
        row = s.query(AcquiredTrack).filter(AcquiredTrack.tkey == tk).first()
    qr = int(quality_rank or 0)
    if row is None:
        s.add(AcquiredTrack(tkey=tk, isrc=iso, artist=(artist or "")[:480],
                            title=(title or "")[:480], quality_rank=qr,
                            quality=quality, path=path))
    else:
        if qr >= int(row.quality_rank or 0):
            row.quality_rank = qr
            if path:
                row.path = path
            if quality:
                row.quality = quality
        if iso and not row.isrc:
            row.isrc = iso
        if tk and not row.tkey:
            row.tkey = tk
    return True


def _ledger_is_dupe(s, path, new_rank=None):
    """(is_dupe, artist, title, isrc, rank) for a freshly-downloaded file. is_dupe is True
    only when we already have this song at >= its quality. Unidentifiable files (no title
    and no ISRC) are NEVER flagged, so we never silently drop something we can't recognise."""
    artist, title, isrc, rank = _file_song_info(path)
    if not (str(title).strip() or str(isrc).strip()):
        return (False, artist, title, isrc, rank)
    if not _dedup_enabled():
        return (False, artist, title, isrc, rank)
    have = _ledger_have_quality(s, artist, title, isrc)
    eff = int((new_rank if new_rank is not None else rank) or 0)
    return (have is not None and have >= eff, artist, title, isrc, rank)


def _ledger_rank_for_row(s, track_ids, fallback_artist=""):
    """Best quality_rank we already have for ANY of a row's tracks — resolved via the liked
    songs / album-track mirrors (track_id → artist/title/ISRC) then the ledger. None when we
    hold none of them. Lets the picker tell 'already own this' from 'genuinely missing'."""
    if not _dedup_enabled():
        return None
    from .db import SpotifyLikedTrack, SpotifyAlbumTrack
    best = None
    for tid in (track_ids or [])[:40]:
        r = None
        try:
            lt = s.query(SpotifyLikedTrack).filter(SpotifyLikedTrack.spotify_track_id == tid).first()
            if lt is not None:
                r = _ledger_have_quality(s, lt.artist or fallback_artist, lt.title, lt.isrc)
            else:
                at = s.query(SpotifyAlbumTrack).filter(SpotifyAlbumTrack.track_id == tid).first()
                if at is not None:
                    r = _ledger_have_quality(s, fallback_artist, at.title, at.isrc)
        except Exception:
            r = None
        if r is not None:
            best = max(best if best is not None else -1, r)
    return best


def _title_from_filename(basename_noext, artist=""):
    """Best-effort song title from a library filename ('NN - Title', 'Artist - Title', …)."""
    b = (basename_noext or "").strip().strip('_').strip()
    b = re.sub(r'^.*?\s+-\s+\d{1,3}\s+-\s+', '', b)        # "Album - 03 - Title"
    b = re.sub(r'^\s*\d{1,3}\s*[-._)]?\s+', '', b)         # leading track number
    b = re.sub(r'_\d{6,}$', '', b)                         # soulseek hash suffix
    b = re.sub(r'\s*\((?:FLAC|MP3|WAV|ALAC)[^)]*\)\s*$', '', b, flags=re.I)
    al = (artist or "").strip().lower()
    if al and b.lower().endswith(" - " + al):
        b = b[:-(len(al) + 3)].strip()
    elif al and b.lower().startswith(al + " - "):
        b = b[len(al) + 3:].strip()
    return b.strip().strip('_').strip()


_LEDGER_BACKFILL_LAST = 0.0
_LEDGER_BACKFILL_MIN_INTERVAL = 90.0  # seconds


def _ledger_backfill_step(limit_rows: int = 40) -> int:
    """Teach the ledger about songs we ALREADY have — cheaply, from the DB + filenames only
    (no file reads). Walks imported AutofillActions past a stored id cursor; idempotent.
    Returns the number of rows processed this step."""
    if not _dedup_enabled():
        return 0
    # THROTTLE: picker_tick calls this every ~10s, and those ticks overlap ~dozens-deep at
    # high concurrency. One write transaction per tick × N concurrent workers saturates
    # SQLite's single write lock and starves the row-claim (UPDATE ... status='downloading'),
    # which then fails with "database is locked" and NO download starts. This backfill only
    # needs to drain a cursor a few times a minute — once per 90s clears the whole queue
    # within minutes without contending with live acquisitions. (Process-global guard; the
    # tiny check-then-set race is harmless — worst case two workers run one extra batch.)
    global _LEDGER_BACKFILL_LAST
    _now_bf = _time_provider.time()
    if _now_bf - _LEDGER_BACKFILL_LAST < _LEDGER_BACKFILL_MIN_INTERVAL:
        return 0
    _LEDGER_BACKFILL_LAST = _now_bf
    try:
        cursor = int(get_config("autofill_ledger_backfill_cursor", "0") or "0")
    except Exception:
        cursor = 0
    done = 0
    last_id = cursor
    try:
        with session_scope() as s:
            rows = list(s.scalars(
                select(AutofillAction)
                .where(AutofillAction.status == "imported")
                .where(AutofillAction.id > cursor)
                .where(AutofillAction.imported_paths.isnot(None))
                .order_by(AutofillAction.id)
                .limit(limit_rows)
            ).all())
            for r in rows:
                last_id = r.id
                try:
                    paths = json.loads(r.imported_paths or "[]") or []
                except Exception:
                    paths = []
                rank = _quality_rank(r.quality_acquired)
                for p in paths:
                    base = os.path.splitext(os.path.basename(p))[0]
                    isrc = base if _ISRC_FILE_RE.match(base.strip()) else ""
                    title = _title_from_filename(base, r.artist or "")
                    _ledger_remember(s, r.artist or "", title, isrc, rank, path=p,
                                     quality=r.quality_acquired)
                done += 1
            s.commit()
        # DB-LOCK FIX (#4): advance the cursor in a SEPARATE transaction AFTER `s` has
        # committed + closed. set_config() opens its OWN session+commit; calling it while the
        # ledger-write transaction above was still open made two writers (the outer txn and
        # set_config's commit) contend on SQLite's single write lock — set_config then hit
        # the 60s busy_timeout and threw "database is locked". The backfill is idempotent, so
        # a crash between the two commits just re-processes a batch next run (harmless).
        if rows:
            set_config("autofill_ledger_backfill_cursor", str(last_id))
    except Exception:
        log.exception("_ledger_backfill_step failed")
    return done


def get_recent_outcomes(within_s: int = 30) -> list:
    """Outcomes recorded in the last `within_s` seconds (also self-prunes the TTL)."""
    now = _time_provider.time()
    out = []
    with _RECENT_OUTCOMES_LOCK:
        for k in list(_RECENT_OUTCOMES.keys()):
            v = _RECENT_OUTCOMES[k]
            age = now - v.get("ts", 0)
            if age > _OUTCOMES_TTL_S:
                _RECENT_OUTCOMES.pop(k, None)
                continue
            if age <= within_s:
                d = dict(v)
                d["age_s"] = round(age, 1)
                out.append(d)
    return out


# 1/2-liked acquisition bias — cache of the user's liked track-ids (refreshed every 5 min)
# plus a round-robin counter so every OTHER pick prefers a Spotify-liked row.
_LIKED_IDS_CACHE = {"ids": None, "ts": 0.0}
_PICK_RATIO_COUNTER = 0
# Smart split between fetching new liked songs vs filling out partial albums.
_FILLER_COUNTER = 0


def _smart_split_should_fill(queue_depth: int) -> bool:
    """Decide whether THIS picker turn should do album-filler (completion) instead of a
    fresh liked-song acquire. Adaptive rather than a fixed 50/50:

      • No new liked songs queued        → fill (acquire has nothing to do).
      • Neither completion source can deliver → acquire. Completion now runs on
        SpotiFLAC (whole-album) AND Telegram (per-track FLAC via @BeatSpotBot). Only when
        BOTH are unavailable do we skip the fill turn (it'd just burn 90-180s failing);
        acquires at least try Soulseek first (a separate, peer-to-peer source).
      • A completion source is live and there's work → ~50/50 alternation.

    (When a fill turn finds no partial album to work on, picker_tick falls through to a
    normal acquire, so a slot is never wasted either way.)
    """
    global _FILLER_COUNTER
    if queue_depth <= 0:
        return True
    try:
        sf_ok = int(get_provider_success_counts_1h().get("spotiflac", 0) or 0) > 0
    except Exception:
        sf_ok = True
    # Telegram can also complete albums (per-track FLAC), so a fill turn is worthwhile
    # whenever EITHER source can deliver — completion is no longer SpotiFLAC-only.
    tg_ok = False
    try:
        from . import telegram_picker as _tgp
        tg_ok = _tgp.is_configured()
    except Exception:
        tg_ok = False
    if not sf_ok and not tg_ok:
        return False
    # Fill balance: AUTO (default) reads the live state of the work — how many
    # NEW songs wait vs how many albums are still filling — and splits turns
    # proportionally (clamped 1..3 of 4 so neither side ever starves). Manual
    # mode uses the dashboard slider verbatim: 0=all album-completion … 4=all
    # new-track acquisition.
    _fill_per_4 = _effective_fill_per_4(queue_depth)
    _FILLER_COUNTER += 1
    return (_FILLER_COUNTER % 4) < _fill_per_4


_FILL_AUTO_CACHE = {"ts": 0.0, "filling": 0}

def _effective_fill_per_4(queue_depth: int) -> int:
    """How many of every 4 picker turns should top up albums (vs fetch new)."""
    mode = (get_config("autofill_fill_mode", "auto") or "auto").lower()
    if mode != "auto":
        try:
            _bal = max(0, min(4, int(get_config("autofill_fill_balance", "2") or "2")))
        except Exception:
            _bal = 2
        return (4, 3, 2, 1, 0)[_bal]
    # AUTO: proportional to the live workload (counts cached 60s)
    now = time.time()
    if now - _FILL_AUTO_CACHE["ts"] > 60:
        try:
            from .db import SessionLocal, AutofillAction
            with SessionLocal() as s_:
                _FILL_AUTO_CACHE["filling"] = s_.query(AutofillAction).filter(
                    AutofillAction.status == "imported").count()
        except Exception:
            pass
        _FILL_AUTO_CACHE["ts"] = now
    filling = _FILL_AUTO_CACHE["filling"]
    total = max(1, filling + max(0, queue_depth))
    share = filling / total
    return max(1, min(3, round(4 * share)))

def _get_liked_track_ids() -> set:
    """Set of the user's liked Spotify track-ids, cached 5 min (drives the 2/3 bias)."""
    now = _time_provider.time()
    if _LIKED_IDS_CACHE["ids"] is not None and (now - _LIKED_IDS_CACHE["ts"]) < 300:
        return _LIKED_IDS_CACHE["ids"]
    ids = set()
    try:
        from .db import SessionLocal, SpotifyLikedTrack
        with SessionLocal() as s:
            ids = {r[0] for r in s.execute(select(SpotifyLikedTrack.spotify_track_id)).all()}
    except Exception:
        log.exception("_get_liked_track_ids failed")
    _LIKED_IDS_CACHE["ids"] = ids
    _LIKED_IDS_CACHE["ts"] = now
    return ids


def _get_installed_spotiflac_commit() -> str | None:
    """Read pip's recorded git ref from direct_url.json (if pip installed via git+)."""
    try:
        import json as _json_p, os as _os_p, subprocess as _sp
        # Find the SpotiFLAC dist-info via pip
        r = _sp.run(["pip", "show", "SpotiFLAC"], capture_output=True, text=True, timeout=10)
        location = None
        for line in r.stdout.splitlines():
            if line.startswith("Location:"):
                location = line.split(":", 1)[1].strip()
                break
        if not location:
            return None
        # Look for SpotiFLAC-*.dist-info directory
        for entry in _os_p.listdir(location):
            if entry.lower().startswith("spotiflac-") and entry.endswith(".dist-info"):
                durl = _os_p.path.join(location, entry, "direct_url.json")
                if _os_p.path.isfile(durl):
                    with open(durl) as f:
                        d = _json_p.load(f)
                    vcs = (d.get("vcs_info") or {})
                    commit = vcs.get("commit_id") or vcs.get("resolved_revision")
                    return commit
        return None
    except Exception:
        log.exception("_get_installed_spotiflac_commit: failed")
        return None


def _get_upstream_spotiflac_commit() -> str | None:
    try:
        import urllib.request as _ur, json as _json_u
        req = _ur.Request(
            "https://api.github.com/repos/%s/branches/main" % (get_config("spotiflac_repo", "ShuShuzinhuu/SpotiFLAC-Module-Version") or "ShuShuzinhuu/SpotiFLAC-Module-Version"),
            headers={"User-Agent": "plexify-health-check"},
        )
        body = _ur.urlopen(req, timeout=10).read()
        d = _json_u.loads(body)
        return d["commit"]["sha"]
    except Exception:
        return None


def patch_spotiflac_future_annotations() -> int:
    """Work around SpotiFLAC-main's recurring 'NameError: <typing name> is not defined'
    (it uses Optional/List/… in annotations without importing them, and without
    `from __future__ import annotations`, so the class body raises at IMPORT time).

    Prepend `from __future__ import annotations` to every SpotiFLAC module missing it,
    across ALL copies on sys.path (the baked /usr/local AND the auto-update's user-site
    /tmp/.local). Idempotent, best-effort per file. Returns how many files were patched.
    """
    import os as _o, sys as _sys
    roots, seen, patched = [], set(), 0
    for sp in list(_sys.path):
        try:
            cand = _o.path.join(sp, "SpotiFLAC")
            if _o.path.isdir(cand):
                roots.append(cand)
        except Exception:
            pass
    for root in roots:
        try:
            rp = _o.path.realpath(root)
        except Exception:
            continue
        if rp in seen or not _o.path.isdir(rp):
            continue
        seen.add(rp)
        for dp, _dirs, fs in _o.walk(rp):
            for fn in fs:
                if not fn.endswith(".py"):
                    continue
                p = _o.path.join(dp, fn)
                try:
                    s = open(p, encoding="utf-8", errors="ignore").read()
                    if "from __future__ import annotations" in s:
                        continue
                    with open(p, "w", encoding="utf-8") as f:
                        f.write("from __future__ import annotations\n" + s)
                    patched += 1
                except PermissionError:
                    pass  # baked copy is root-owned; the build-time patch handles it
                except Exception:
                    log.exception("patch_spotiflac: failed on %s", p)
    if patched:
        log.warning("patch_spotiflac_future_annotations: patched %d SpotiFLAC module(s)", patched)
    return patched


def spotiflac_health_check_tick() -> dict:
    """Runs every 30min. If SpotiFLAC is broken (0 successes in last hour) AND
    upstream main has a newer commit than installed, trigger an in-place
    pip-upgrade + container restart.
    """
    import subprocess as _sp_h, threading as _th_h, os as _os_h, signal as _sig_h, time as _t_h
    result = {"action": "none"}
    counts = get_provider_success_counts_1h()
    sf_ok = counts.get("spotiflac", 0)
    sl_ok = counts.get("soulseek", 0)
    result.update(counts)

    # Sanity: only treat zero-success as a "broken" signal if we've actually tried.
    # If picker has been idle (queue empty), don't update prematurely.
    queued_depth = 0
    try:
        from .db import SessionLocal, AutofillAction
        with SessionLocal() as s:
            queued_depth = s.query(AutofillAction).filter(AutofillAction.status == "queued").count()
    except Exception:
        pass
    result["queued"] = queued_depth

    if sf_ok > 0:
        log.info("spotiflac_health: healthy (%d SpotiFLAC successes / %d Soulseek successes in 1h)", sf_ok, sl_ok)
        result["status"] = "healthy"
        return result
    if queued_depth < 10:
        log.info("spotiflac_health: queue depth %d < 10 — picker is idle, skipping update check", queued_depth)
        result["status"] = "idle"
        return result

    log.warning("spotiflac_health: SpotiFLAC broken (0 successes/1h, %d Soulseek). Checking upstream...", sl_ok)
    installed = _get_installed_spotiflac_commit()
    upstream = _get_upstream_spotiflac_commit()
    result["installed_commit"] = installed
    result["upstream_commit"] = upstream

    if not upstream:
        log.warning("spotiflac_health: can't reach GitHub — skipping update check")
        result["status"] = "github_unreachable"
        return result

    if installed and installed.startswith(upstream[:10]):
        log.info("spotiflac_health: broken but already on latest upstream commit %s — nothing to do", upstream[:10])
        result["status"] = "already_latest"
        return result

    log.warning("spotiflac_health: triggering in-place pip-upgrade (installed=%s upstream=%s)",
                installed and installed[:10], upstream[:10])
    try:
        _sp_h.run([
            "pip", "install", "--upgrade", "--no-deps", "--force-reinstall",
            "git+https://github.com/%s.git@main" % (get_config("spotiflac_repo", "ShuShuzinhuu/SpotiFLAC-Module-Version") or "ShuShuzinhuu/SpotiFLAC-Module-Version"),
        ], capture_output=True, text=True, timeout=180, check=True)
    except Exception as e:
        log.exception("spotiflac_health: pip install failed")
        result["status"] = "pip_failed"
        result["error"] = str(e)[:200]
        return result

    # Self-heal the recurring git-main 'NameError: Optional' regression before verifying.
    try:
        patch_spotiflac_future_annotations()
    except Exception:
        log.exception("spotiflac_health: post-update patch failed (continuing to verify)")

    # Verify the freshly-installed SpotiFLAC actually imports before we restart into it —
    # a broken update (new undeclared dependency, etc.) must NOT crash-loop the picker.
    try:
        _imp = _sp_h.run(["python", "-c", "import SpotiFLAC"], capture_output=True, text=True, timeout=30)
        if _imp.returncode != 0:
            _ierr = (_imp.stderr or "")
            # SMART UPDATE: the deterministic patch didn't fix it — ask Claude (Opus) to
            # repair the broken module(s). Only runs when an API key is configured; it
            # verifies the import itself and rolls back if it can't fix it.
            _smart_fixed = False
            try:
                from . import smart_update as _su
                if _su.is_configured():
                    log.warning("spotiflac_health: deterministic patch failed — invoking Claude smart-repair")
                    _rep = _su.llm_repair(_ierr)
                    result["smart_update"] = {k: _rep.get(k) for k in ("applied", "files_changed", "error", "model")}
                    _smart_fixed = bool(_rep.get("applied"))
            except Exception:
                log.exception("spotiflac_health: smart-update repair crashed (continuing)")
            if not _smart_fixed:
                log.error("spotiflac_health: updated SpotiFLAC fails to import — NOT restarting:\n%s",
                          _ierr[-500:])
                result["status"] = "update_broken_import"
                result["error"] = _ierr[-200:]
                return result
            log.warning("spotiflac_health: Claude smart-repair fixed the import")
    except Exception:
        log.exception("spotiflac_health: import verification crashed — NOT restarting")
        result["status"] = "verify_failed"
        return result
    log.warning("spotiflac_health: install succeeded + verified; scheduling graceful restart in 5s")
    def _restart_later():
        _t_h.sleep(5)
        try:
            _os_h.kill(1, _sig_h.SIGTERM)
        except Exception:
            log.exception("spotiflac_health: SIGTERM to PID 1 failed")
    _th_h.Thread(target=_restart_later, name="health-restart", daemon=True).start()
    result["status"] = "updated_restarting"
    return result


def is_picker_in_cooldown() -> dict:
    """Read-only check of circuit-breaker state. Used by /api/picker/status."""
    from datetime import datetime as _dt4
    if _PICKER_COOLDOWN_UNTIL is None:
        return {"in_cooldown": False, "until": None, "seconds_remaining": 0}
    now = _dt4.utcnow()
    if now >= _PICKER_COOLDOWN_UNTIL:
        return {"in_cooldown": False, "until": None, "seconds_remaining": 0}
    return {
        "in_cooldown": True,
        "until": _PICKER_COOLDOWN_UNTIL.isoformat() + "Z",
        "seconds_remaining": int((_PICKER_COOLDOWN_UNTIL - now).total_seconds()),
    }



# ── Progress watcher: polls per-acquisition dest_dir for *.flac count
#    and updates CURRENT_ACQUISITIONS[row_id]['tracks_done'] live so the
#    dashboard can render a real fraction.
def _start_progress_watcher(row_id: int, acq_dir: str, tracks_total: int, stop_event):
    import glob as _glob, threading as _t, os as _os
    def _loop():
        try:
            while not stop_event.wait(2.0):
                try:
                    if not _os.path.isdir(acq_dir):
                        continue
                    flacs = _glob.glob(_os.path.join(acq_dir, "**", "*.flac"), recursive=True)
                    done = len(flacs)
                    # Cap at total so we never display >100%.
                    if tracks_total and tracks_total > 0:
                        done = min(done, tracks_total)
                    with _ACQUISITION_LOCK:
                        if row_id in CURRENT_ACQUISITIONS:
                            CURRENT_ACQUISITIONS[row_id]['tracks_done'] = done
                except Exception:
                    pass
        except Exception:
            pass
    th = _t.Thread(target=_loop, name=f"progress-watcher-{row_id}", daemon=True)
    th.start()
    return th


CURRENT_ACQUISITION: Optional[dict] = None

# ── SMART download concurrency ────────────────────────────────────────────────
# Replaces the old manual "slots" knob. The picker concurrency auto-tunes itself
# (AIMD: additive-increase on clean success, multiplicative-decrease on rate-limits/
# failures), bounded by a ceiling. It's tuned to how the two backends actually work:
#   • SpotiFLAC pulls from rate-limited HTTP mirrors → too many lanes triggers 429/503,
#     so we back off hard the moment failures cluster.
#   • Soulseek/slskd manages its own per-peer transfer parallelism, so extra lanes just
#     mean more simultaneous searches+transfers → we push the ceiling higher when it's
#     the source that's delivering.
_SMART_CONC = {"value": None}
_CONC_FLOOR = 2


def _conc_ceiling() -> int:
    # Higher ceiling when Soulseek is the live primary (it parallelizes internally and
    # isn't the one getting rate-limited); tighter when leaning on SpotiFLAC mirrors.
    try:
        base = int(get_config("autofill_max_concurrency", "10") or "10")
    except Exception:
        base = 10
    try:
        counts = get_provider_success_counts_1h()
        if int(counts.get("soulseek", 0) or 0) > int(counts.get("spotiflac", 0) or 0):
            base = max(base, 14)
    except Exception:
        pass
    return max(_CONC_FLOOR, base)


def _smart_concurrency() -> int:
    """Current auto-tuned download concurrency (read-only — the single source of truth)."""
    v = _SMART_CONC.get("value")
    if v is None:
        try:
            v = int(get_config("autofill_start_concurrency", "6") or "6")
        except Exception:
            v = 6
        _SMART_CONC["value"] = max(_CONC_FLOOR, v)
    return int(_SMART_CONC["value"])


def _adjust_smart_concurrency() -> int:
    """One AIMD step — called once per watchdog cycle (60s)."""
    cur = _smart_concurrency()
    ceil = _conc_ceiling()
    try:
        recent = list(_PICKER_OUTCOMES)
    except Exception:
        recent = []
    fails = sum(1 for o in recent if o == "provider_failure")
    succ = sum(1 for o in recent if o == "success")
    if recent:
        if fails >= max(2, len(recent) // 2):
            cur = max(_CONC_FLOOR, cur // 2)        # multiplicative decrease on trouble
        elif succ >= 1 and fails == 0:
            cur = min(ceil, cur + 1)                # additive increase when clean
    cur = max(_CONC_FLOOR, min(ceil, cur))
    _SMART_CONC["value"] = cur
    return cur


def _max_picker_concurrency() -> int:
    return _smart_concurrency()


def _max_picker_attempts() -> int:
    """Albums that have failed this many times get auto-abandoned. Default 8."""
    try:
        return max(1, int(get_config("autofill_max_picker_attempts", "8") or 8))
    except (ValueError, TypeError):
        return 8


# ===== Tag-verified placement (metadata correctness) =====

def _read_file_tags(fpath: str) -> dict:
    """Read artist/albumartist/album/title from a FLAC. Returns dict; empty on error."""
    try:
        from mutagen.flac import FLAC
        f = FLAC(fpath)
        tags = f.tags or {}
        def _g(k):
            v = tags.get(k)
            if not v:
                return ""
            return str(v[0]) if isinstance(v, list) else str(v)
        return {
            "artist": _g("artist").strip(),
            "albumartist": _g("albumartist").strip(),
            "album": _g("album").strip(),
            "title": _g("title").strip(),
            "tracknumber": _g("tracknumber").strip(),
        }
    except Exception:
        return {}


# Soundtrack / cast / compilation indicators in an album title — when the mirror
# doesn't know the album, these strongly imply a multi-performer album whose tracks
# should all share one album artist ("Various Artists") instead of fragmenting per
# performer. Kept conservative on purpose (no generic words like "live"/"hits").
_SOUNDTRACK_KW = [
    "soundtrack", "motion picture", "original cast", "original score", "ost",
    "the musical", "broadway", "music from", "songs from", "original television",
    "cast recording", "from the motion picture",
]


def _aa_consistent(cand, row_artist):
    """Guard: True only if a looked-up album-artist plausibly belongs to THIS row's artist
    (same artist, a multi-artist superset, or Various Artists). Stops an album-NAME collision
    from filing a song under a totally different artist (e.g. Telehope's 'On Your Own' -> Blur)."""
    if not cand:
        return False
    c = cand.strip().lower()
    if c in ("various artists", "various", "va"):
        return True
    cn = _normalize_for_key(cand); rn = _normalize_for_key(row_artist or "")
    if not rn:
        return True
    if rn in cn or cn in rn or (set(cn.split()) & set(rn.split())):
        return True
    try:
        from rapidfuzz.fuzz import token_set_ratio
        return token_set_ratio(cn, rn) >= 55
    except Exception:
        return False


def _canonical_album_artist(artist, album, track_ids=None, foreign_album_id=None):
    """Decide the album artist a track's files should be placed/tagged under, so a
    multi-performer album groups as ONE album in Plex instead of one tile per
    performer. Returns (album_artist, confident).

      0. MusicBrainz cache (the 'mdb' — authoritative): an external music database
         that KNOWS the canonical album artist (incl. 'Various Artists' for
         soundtracks/compilations) instead of inferring it. Read-only/local here;
         populated by the background mb_enrich_tick.
      1. Spotify mirror: the Spotify album's own album_artist — also authoritative
         and consistent across the album, used when MB hasn't enriched it yet.
      2. Soundtrack/cast keyword in the album title -> 'Various Artists'.
      3. Fallback: the track artist as-is (current behaviour). NOT forced onto the
         tag, so a well-tagged '&'-band ('Simon & Garfunkel') is never mangled.
    """
    # 0a. Disk: if a Various Artists folder for this album ALREADY exists in the
    # library, new tracks belong there — never a fresh per-track-artist folder.
    try:
        _va_dir = "/Volumes/MediaVolume3/plexify-music/Various Artists/%s" % _safe_for_fs(album or "", "")
        if album and os.path.isdir(_va_dir) and any(
                f.lower().endswith((".flac", ".mp3", ".m4a")) for f in os.listdir(_va_dir)):
            return "Various Artists", True
    except Exception:
        pass
    # 0. MusicBrainz cache — authoritative, no network (background tick fills it).
    try:
        from .db import SessionLocal, MbAlbumMeta
        ak = _normalize_for_key(artist or "")
        bk = _normalize_for_key(album or "")
        if bk:
            with SessionLocal() as s:
                m = s.get(MbAlbumMeta, (ak, bk))
                if m and m.status == "ok" and (m.album_artist or "").strip() and _aa_consistent(m.album_artist, artist):
                    return m.album_artist.strip(), True
    except Exception:
        log.exception("_canonical_album_artist: MB cache read failed")
    try:
        from .db import SessionLocal, SpotifyAlbum, SpotifyAlbumTrack
        with SessionLocal() as s:
            alb = None
            if foreign_album_id and str(foreign_album_id).startswith("sp:"):
                alb = s.query(SpotifyAlbum).filter(
                    SpotifyAlbum.album_id == foreign_album_id[3:]).first()
            if alb is None and track_ids:
                for tid in track_ids:
                    t = s.query(SpotifyAlbumTrack).filter(
                        SpotifyAlbumTrack.track_id == tid).first()
                    if t:
                        alb = s.query(SpotifyAlbum).filter(
                            SpotifyAlbum.album_id == t.album_id).first()
                        if alb:
                            break
            if alb and (alb.album_artist or "").strip() and _aa_consistent(alb.album_artist, artist):
                return alb.album_artist.strip(), True
    except Exception:
        log.exception("_canonical_album_artist: mirror lookup failed")
    nl = (album or "").lower()
    if any(kw in nl for kw in _SOUNDTRACK_KW):
        return "Various Artists", True
    return (artist or "Unknown Artist"), False


_MB_ENRICH_LOCK = threading.Lock()


def mb_enrich_tick(batch: int = 12) -> dict:
    """Gentle background MusicBrainz enrichment — the ONLY live MB caller.

    Looks up the canonical album artist for albums we have (imported first, then
    queued) and caches it in mb_album_meta, so the picker's _canonical_album_artist
    reads an authoritative answer locally instead of inferring it. Rate-limited to
    ~1 MB request/sec inside the client; single-flight; small batch per tick.
    """
    result = {"looked_up": 0, "ok": 0, "various": 0, "notfound": 0, "skipped": False}
    if not _MB_ENRICH_LOCK.acquire(blocking=False):
        result["skipped"] = True
        return result
    try:
        from .db import SessionLocal, AutofillAction, MbAlbumMeta
        from . import musicbrainz as _mb
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        now = _dt.now(_tz.utc)

        todo = []
        with SessionLocal() as s:
            have = {(m.artist_key, m.album_key): m for m in s.query(MbAlbumMeta).all()}
            rows = s.query(AutofillAction).filter(
                AutofillAction.album.isnot(None), AutofillAction.album != "",
                AutofillAction.artist.isnot(None),
            ).all()
            # imported first, then queued, then everything else
            order = {"imported": 0, "queued": 1}
            rows.sort(key=lambda r: order.get(r.status, 2))
            seen = set()
            for r in rows:
                ak = r.artist_key or _normalize_for_key(r.artist or "")
                bk = r.album_key or _normalize_for_key(r.album or "")
                if not bk or (ak, bk) in seen:
                    continue
                seen.add((ak, bk))
                ex = have.get((ak, bk))
                if ex:
                    if ex.status == "ok":
                        continue
                    if ex.status in ("notfound", "error") and ex.fetched_at:
                        ft = ex.fetched_at
                        if ft.tzinfo is None:
                            ft = ft.replace(tzinfo=_tz.utc)
                        # exponential backoff: retry misses less often over time
                        cooldown = _td(days=min(30, 2 ** min(ex.attempts or 1, 5)))
                        if now - ft < cooldown:
                            continue
                todo.append((ak, bk, r.artist, r.album))
                if len(todo) >= batch:
                    break

        for ak, bk, artist, album in todo:
            result["looked_up"] += 1
            try:
                info = _mb.mb_lookup_album(artist, album)
            except Exception:
                log.exception("mb_enrich_tick: lookup raised for %s / %s", artist, album)
                info = None
            with SessionLocal() as s:
                m = s.get(MbAlbumMeta, (ak, bk))
                is_new = m is None
                if is_new:
                    m = MbAlbumMeta(artist_key=ak, album_key=bk)
                m.query_artist = artist
                m.query_album = album
                m.attempts = (m.attempts or 0) + 1
                m.fetched_at = _dt.now(_tz.utc)
                if info and info.get("album_artist"):
                    m.album_artist = info["album_artist"]
                    m.album_artist_mbid = info.get("album_artist_mbid")
                    m.is_various = bool(info.get("is_various"))
                    m.release_group_mbid = info.get("release_group_mbid")
                    m.primary_type = info.get("primary_type")
                    m.secondary_types = info.get("secondary_types")
                    m.score = info.get("score")
                    m.status = "ok"
                    result["ok"] += 1
                    if m.is_various:
                        result["various"] += 1
                else:
                    m.status = "notfound"
                    result["notfound"] += 1
                if is_new:
                    s.add(m)
                s.commit()
    except Exception:
        log.exception("mb_enrich_tick: unexpected failure")
    finally:
        try:
            _MB_ENRICH_LOCK.release()
        except Exception:
            pass
    log.info("mb_enrich_tick: %s", result)
    return result


def _strip_artist_prefix_from_title(title, artist):
    """Return the song title with a leading '<artist> - ' (or :/|) prefix removed.

    Soulseek peers often tag the TITLE field as 'OneRepublic - Counting Stars'.
    We strip the artist prefix ONLY when it's followed by a real separator (dash/
    colon/pipe), so a song genuinely titled '<word> <word>' is never touched."""
    import re as _re
    na = _re.sub(r"[^a-z0-9]", "", (artist or "").lower())
    t = title or ""
    if not na or len(na) < 2 or not t:
        return t
    i = consumed = 0
    while i < len(t) and consumed < len(na):
        ch = t[i]
        if ch.isalnum():
            if ch.lower() == na[consumed]:
                consumed += 1
            else:
                return t                 # front doesn't match the artist -> leave it
            i += 1
        else:
            i += 1                       # skip the title's own spaces/punct
    if consumed < len(na):
        return t
    m = _re.match(r"^\s*[\-\u2013\u2014:|/]+\s*(.+)$", t[i:])
    if m and m.group(1).strip():
        return m.group(1).strip()
    return t


def _stamp_file_tags(path: str, artist: str, album: str, album_artist: str = None) -> bool:
    """Fill EMPTY embedded tags from the album/artist the picker explicitly asked
    for, so Plex never shows '[Unknown Album]'.

    WHY: Soulseek peers (and some degraded provider fallbacks) deliver FLACs with
    no Vorbis comments — just a filename. Plex groups by embedded tag, so a blank
    album tag becomes '[Unknown Album]' and a blank albumartist splits one album
    into many tiles. We TRUST the row context (the picker requested this exact
    album), so stamp it on. Only fills blanks — never overwrites a good tag.

    album_artist: when provided, this is the CANONICAL album artist (from the
    Spotify mirror, or 'Various Artists' for a soundtrack/compilation). It is
    FORCE-written so every track of a multi-performer album shares one albumartist
    and Plex groups them as a single album — that's the fix for soundtracks like
    Dr. Horrible fragmenting into one tile per performer. The per-track `artist`
    (the performer) is preserved. When album_artist is None we fall back to the
    old fill-from-artist behaviour.
    Best-effort: never raises into the placement path. Returns True if it wrote."""
    try:
        from mutagen.flac import FLAC
        f = FLAC(path)

        def _empty(k):
            v = f.get(k)
            return not (v and str(v[0]).strip())

        changed = {}
        # ALBUM + ALBUMARTIST are FORCE-written to one consistent value for the whole folder.
        # This is the fix for Plex splitting a single album into many tiles: providers deliver
        # tracks with mismatched album tags (edition variants, a comp name) or a per-track
        # albumartist, and Plex groups strictly by (album, albumartist). The picker always
        # knows the canonical album for this folder, so we overwrite — never just fill.
        if album and album.strip():
            cur = f.get("album")
            if not (cur and str(cur[0]).strip() == album.strip()):
                changed["album"] = album.strip()
        # One albumartist for every track: the canonical (Various Artists for soundtracks) when
        # known, else the row's album artist. The per-track performer (`artist`) is preserved.
        _aa = (album_artist.strip() if (album_artist and album_artist.strip())
               else (artist.strip() if (artist and artist.strip()) else ""))
        if _aa:
            cur = f.get("albumartist")
            if not (cur and str(cur[0]).strip() == _aa):
                changed["albumartist"] = _aa
        if artist and _empty("artist"):
            changed["artist"] = artist
        if _empty("title"):
            base = os.path.splitext(os.path.basename(path))[0]
            # peer/single naming "<title> - <artist>": strip the trailing artist
            if artist and base.lower().rstrip().endswith(artist.lower().strip()):
                cut = base[: -len(artist)].rstrip()
                if cut.endswith("-"):
                    cut = cut[:-1].rstrip()
                base = cut or base
            if base:
                changed["title"] = base
        else:
            _ct = str((f.get("title") or [""])[0])
            _fa = str((f.get("artist") or [""])[0]) or (artist or "")
            _clean = _strip_artist_prefix_from_title(_ct, _fa)
            if _clean and _clean != _ct.strip():
                changed["title"] = _clean
        if not changed:
            return False
        for k, v in changed.items():
            f[k] = v
        f.save()
        log.info("tag-stamp: filled %s on %s", list(changed.keys()), os.path.basename(path))
        return True
    except Exception:
        log.exception("_stamp_file_tags failed for %s", path)
        return False


# Per-file PROVENANCE: which source actually delivered THIS file. An album can be mixed
# (most from Soulseek, a few filled by squid/Telegram), so the row's single source is not
# enough — we stamp the real source on each file and the dashboard reads it back, so every
# song reports exactly where it came from.
_SOURCE_FIELD = "plexifysource"          # written to new files


def _set_source_tag(path: str, source: str) -> None:
    if not source or not path or not path.lower().endswith(".flac"):
        return
    try:
        from mutagen.flac import FLAC
        f = FLAC(path)
        f[_SOURCE_FIELD] = [str(source).strip().lower()]
        f.save()
    except Exception:
        pass


def enforce_clean_titles_tick(window: int = 150) -> dict:
    """Strip 'Artist - ' prefixes from song TITLE tags across the library on a
    rotating cursor (belt-and-suspenders to the _stamp_file_tags guard). FLAC only."""
    out = {"fixed": 0}
    if (get_config("acq_recovery_enabled", "0") or "0") != "1":
        out["skipped"] = "disabled"
        return out
    try:
        if _plex_active_video_sessions() > 0:
            out["skipped"] = "streaming"
            return out
    except Exception:
        pass
    folders = []
    try:
        for art in sorted(os.listdir(_LOCAL_PATH_PREFIX)):
            ad = os.path.join(_LOCAL_PATH_PREFIX, art)
            if os.path.isdir(ad) and not art.startswith(("_", ".")):
                for alb in sorted(os.listdir(ad)):
                    if os.path.isdir(os.path.join(ad, alb)):
                        folders.append((art, alb))
    except OSError:
        return out
    if not folders:
        return out
    try:
        cur = int(get_config("title_clean_cursor", "0") or "0")
    except Exception:
        cur = 0
    if cur >= len(folders):
        cur = 0
    from mutagen.flac import FLAC
    touched_dirs = set()
    for art, alb in folders[cur:cur + window]:
        d = os.path.join(_LOCAL_PATH_PREFIX, art, alb)
        try:
            files = os.listdir(d)
        except OSError:
            continue
        for fn in files:
            if not fn.lower().endswith(".flac"):
                continue
            fp = os.path.join(d, fn)
            try:
                f = FLAC(fp)
                t = str((f.get("title") or [""])[0])
                a = str((f.get("artist") or f.get("albumartist") or [""])[0])
                if not t:
                    continue
                clean = _strip_artist_prefix_from_title(t, a)
                if clean and clean != t.strip():
                    f["title"] = clean
                    f.save()
                    out["fixed"] += 1
                    touched_dirs.add(d)
            except Exception:
                continue
    set_config("title_clean_cursor", str(cur + window if cur + window < len(folders) else 0))
    if out["fixed"]:
        try:
            from . import plex_client
            srv = plex_client._connect()
            sec = plex_client._music_section(srv) if srv else None
            for d in list(touched_dirs)[:30]:
                try:
                    sec.update(path=d.replace(_LOCAL_PATH_PREFIX, _plex_prefix(), 1))
                except Exception:
                    pass
        except Exception:
            pass
        log.info("enforce_clean_titles_tick: %s", out)
    return out


def _get_source_tag(path: str) -> str:
    try:
        if not path or not path.lower().endswith(".flac"):
            return ""
        from mutagen.flac import FLAC
        _f = FLAC(path)
        v = _f.get(_SOURCE_FIELD)
        return (str(v[0]).strip().lower() if v else "")
    except Exception:
        return ""


def _tags_match_row(tags: dict, row_artist: str, row_album: str) -> tuple:
    """Returns (matches: bool, reason: str). Fuzzy comparison.

    LENIENT on missing tags: when SpotiFLAC delivers a file without metadata
    (tags={} or missing artist/album), TRUST the row context — the picker
    explicitly asked for this album, so accept the file as-is. The original
    purpose of tag-verification was to catch cases where SpotiFLAC delivered
    the WRONG album with VALID-BUT-MISMATCHED tags. Tagless files are ambiguous,
    not wrong.
    """
    if not tags:
        return True, "accept-tagless"
    file_artist = tags.get("artist") or tags.get("albumartist") or ""
    file_album = tags.get("album") or ""
    if not file_artist or not file_album:
        return True, "accept-partial-tags"
    norm_ra = _normalize_for_key(row_artist or "")
    norm_rb = _normalize_for_key(row_album or "")
    norm_fa = _normalize_for_key(file_artist)
    norm_fb = _normalize_for_key(file_album)
    if norm_ra == norm_fa and norm_rb == norm_fb:
        return True, "exact"
    try:
        from rapidfuzz.fuzz import token_set_ratio
        a_score = token_set_ratio(norm_ra, norm_fa)
        b_score = token_set_ratio(norm_rb, norm_fb)
        if a_score >= 85 and b_score >= 80:  # L-8 tightened — was 65/50, accepting too many false matches
            return True, "fuzzy(a=" + str(a_score) + " b=" + str(b_score) + ")"
        # Edition-variant tolerance: when the ARTIST matches strongly, compare
        # album CORES with edition qualifiers stripped, so e.g. "X (Mono)" and
        # "X (45th Anniversary Edition)" still match instead of being rejected as
        # "provider delivered wrong tracks". a_score>=88 guards against wrong artist.
        if a_score >= 88:
            _ed = {"mono", "stereo", "deluxe", "remaster", "remastered", "anniversary",
                   "edition", "expanded", "version", "bonus", "special", "original",
                   "soundtrack", "reissue", "collectors", "collector", "th", "st",
                   "nd", "rd", "disc", "cd", "lp", "vol", "volume"}
            core_rb = " ".join(w for w in norm_rb.split() if not (w in _ed or w.isdigit()))
            core_fb = " ".join(w for w in norm_fb.split() if not (w in _ed or w.isdigit()))
            if core_rb and core_fb and token_set_ratio(core_rb, core_fb) >= 85:
                return True, "fuzzy-edition(a=%d core_b=%d)" % (a_score, token_set_ratio(core_rb, core_fb))
        return False, "fuzzy-low(a=" + str(a_score) + " b=" + str(b_score) + ") row=" + repr(row_artist) + "/" + repr(row_album) + " file=" + repr(file_artist) + "/" + repr(file_album)
    except ImportError:
        if (norm_ra in norm_fa or norm_fa in norm_ra) and (norm_rb in norm_fb or norm_fb in norm_rb):
            return True, "substring"
        return False, "substring-fail"


def download_album_cover(row, dest_dir: str) -> bool:
    """Download a high-res album cover from Spotify and save as cover.jpg.
    Best-effort: returns True if a cover was written, False otherwise."""
    import os
    if not row or not row.track_ids_json:
        return False
    try:
        track_ids = json.loads(row.track_ids_json) or []
    except Exception:
        return False
    if not track_ids:
        return False
    dest = os.path.join(dest_dir, "cover.jpg")
    if os.path.exists(dest):
        return True  # already have one
    try:
        sp = auth_spotify.get_client()
        if not sp:
            return False
        t = spotify_client._retry_patient(sp.track, track_ids[0])
        if not t:
            return False
        album = t.get("album") or {}
        if (getattr(row, "album", "") or "").strip() and not _album_names_match(
                album.get("name", ""), row.album):
            return False  # default-album art != this row's album; better none than wrong
        images = album.get("images") or []
        if not images:
            return False
        # Pick largest (first by Spotify convention)
        url = images[0].get("url")
        if not url:
            return False
        import requests
        resp = requests.get(url, timeout=8)
        if resp.status_code != 200:
            return False
        os.makedirs(dest_dir, exist_ok=True)
        with open(dest, "wb") as fp:
            fp.write(resp.content)
        try:
            os.chmod(dest, 0o664)
        except OSError:
            pass
        log.info("download_album_cover: saved %d bytes to %s", len(resp.content), dest)
        return True
    except Exception:
        log.exception("download_album_cover: failed for row %s", row.id if row else None)
        return False


def _tag_based_placement(paths, row_artist, row_album):
    """Split delivered paths into (passing, failing). Failing is list of (path, reason)."""
    passing = []
    failing = []
    for p in paths:
        tags = _read_file_tags(p)
        ok, reason = _tags_match_row(tags, row_artist, row_album)
        if ok:
            passing.append(p)
        else:
            log.warning("tag mismatch: %s reason=%s tags=%s row=%s/%s",
                        p, reason, tags, row_artist, row_album)
            failing.append((p, reason))
    return passing, failing


def is_pipeline_busy() -> tuple[bool, str]:
    """True only when picker is at max concurrency. Multiple in-flight is OK."""
    with _ACQUISITION_LOCK:
        n = len(CURRENT_ACQUISITIONS)
    max_n = _max_picker_concurrency()
    if n >= max_n:
        return True, f"picker at max concurrency ({n}/{max_n})"
    return False, ""



# ====================================================================
# Normalization helpers (B5, B13)
# ====================================================================
def _normalize_for_key(s: Optional[str]) -> str:
    """Lowercase + NFKD + strip combining marks + strip punctuation + collapse whitespace.

    Used for case/diacritic/punctuation-insensitive dedup of (artist, album) keys.
    - 'Beyoncé' and 'Beyonce' produce the same key.
    - 'Unmasked: The Platinum Collection' and 'Unmasked- The Platinum Collection'
      produce the same key (filesystem-safe transforms replace ':' with '-' etc.).
    - 'AC/DC' and 'ACDC' produce the same key.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    # Strip all punctuation; keep alphanumerics and whitespace. Round-trips
    # filesystem-safe transforms that map :,<,>,",|,?,* -> -.
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return " ".join(s.lower().split())


# ====================================================================
# AUDIT-2: centralized run-persistence with success tagging.
# Every tick exit path goes through this helper instead of calling set_config
# directly. Side-effect: also writes autofill_last_successful_run_json when
# the run finished cleanly (real scan + no source errors) so the UI can
# fall back to the last good numbers when a fresh run failed.
# ====================================================================
def _persist_run(stats: dict) -> None:
    """Persist last run stats. Hardened with raw-SQL fallback in case the ORM
    hits a rare RecursionError under nested-import paths."""
    try:
        had_scan = (stats.get("scanned_tracks") or 0) > 0
        had_errs = bool(stats.get("source_errors"))
        stats["run_ok"] = bool(had_scan and not had_errs)
    except Exception:
        stats["run_ok"] = False
    payload = json.dumps(stats)
    keys_to_write = [("autofill_last_run_json", payload)]
    if stats.get("run_ok"):
        keys_to_write.append(("autofill_last_successful_run_json", payload))
    for key, value in keys_to_write:
        try:
            set_config(key, value)
        except RecursionError:
            log.warning("_persist_run: ORM RecursionError for %s, falling back to raw SQL", key)
            try:
                from .db import engine
                from sqlalchemy import text
                with engine.begin() as conn:
                    conn.execute(
                        text("INSERT INTO app_config (key, value) VALUES (:k, :v) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value"),
                        {"k": key, "v": value},
                    )
            except Exception:
                log.exception("_persist_run raw-SQL fallback failed for %s", key)
        except Exception:
            log.exception("_persist_run failed for %s", key)




# ====================================================================
# SPOTIFY LOCAL CACHE
# Pull Liked Songs into the local DB so the rest of the engine never
# has to call Spotify in the hot path. Resilient to 429 — partial
# pulls upsert what they got and try again next tick.
# ====================================================================
def sync_spotify_liked_tracks_tick(max_pages: int = 200) -> dict:
    """Pull the full Liked Songs list from Spotify into SpotifyLikedTrack.

    Idempotent. Upserts by spotify_track_id. Tracks that have been unliked
    are detected by absence and removed.
    """
    stats = {
        "started_at": datetime.utcnow().isoformat() + "Z",
        "pages_pulled": 0, "tracks_seen": 0,
        "new": 0, "updated": 0, "removed": 0,
        "errors": [],
    }
    sp = auth_spotify.get_client()
    if sp is None:
        stats["errors"].append("Spotify not authed")
        set_config("spotify_liked_sync_last_json", json.dumps(stats))
        return stats

    from .db import SpotifyLikedTrack
    seen_ids: set[str] = set()
    offset = 0
    page = 0
    aborted_partial = False
    while page < max_pages:
        try:
            res = spotify_client._retry_patient(sp.current_user_saved_tracks,
                                                limit=50, offset=offset)
        except Exception as e:
            stats["errors"].append(f"page offset={offset}: {type(e).__name__}: {str(e)[:200]}")
            # Partial — keep what we have, retry next tick
            aborted_partial = True
            break
        if not res:
            break
        items = res.get("items") or []
        if not items:
            break
        page += 1
        stats["pages_pulled"] = page
        # Upsert each
        with session_scope() as s:
            for it in items:
                t = it.get("track") or {}
                tid = t.get("id")
                if not tid:
                    continue
                seen_ids.add(tid)
                stats["tracks_seen"] += 1
                primary = (t.get("artists") or [{}])[0]
                row = s.get(SpotifyLikedTrack, tid)
                added_at_str = it.get("added_at")
                added_at = None
                if added_at_str:
                    try:
                        added_at = datetime.fromisoformat(added_at_str.replace("Z", "+00:00")).replace(tzinfo=None)
                    except Exception:
                        pass
                if row:
                    row.artist = primary.get("name") or row.artist
                    row.album = (t.get("album") or {}).get("name") or row.album
                    row.title = t.get("name") or row.title
                    row.isrc = (t.get("external_ids") or {}).get("isrc") or row.isrc
                    row.duration_ms = t.get("duration_ms") or row.duration_ms
                    row.primary_artist_id = primary.get("id") or row.primary_artist_id
                    if added_at:
                        row.added_at_spotify = added_at
                    row.cached_at = datetime.utcnow()
                    stats["updated"] += 1
                else:
                    s.add(SpotifyLikedTrack(
                        spotify_track_id=tid,
                        artist=primary.get("name") or "",
                        album=(t.get("album") or {}).get("name") or "",
                        title=t.get("name") or "",
                        isrc=(t.get("external_ids") or {}).get("isrc"),
                        duration_ms=t.get("duration_ms"),
                        primary_artist_id=primary.get("id"),
                        added_at_spotify=added_at,
                        cached_at=datetime.utcnow(),
                    ))
                    stats["new"] += 1
        # Pagination
        nxt = res.get("next")
        if not nxt:
            break
        offset += len(items)

    # Removal sweep: only if we got a complete pull (no errors)
    if not aborted_partial and seen_ids:
        with session_scope() as s:
            current = {r.spotify_track_id for r in s.scalars(select(SpotifyLikedTrack)).all()}
            stale = current - seen_ids
            if stale:
                for tid in list(stale)[:5000]:  # safety cap
                    row = s.get(SpotifyLikedTrack, tid)
                    if row:
                        s.delete(row)
                        stats["removed"] += 1

    stats["finished_at"] = datetime.utcnow().isoformat() + "Z"
    stats["aborted_partial"] = aborted_partial
    # Mirror cache state into LocalTrack rows for the sentinel pair (only on complete pulls)
    if not aborted_partial:
        try:
            _sync_liked_songs_local_tracks(stats)
        except Exception:
            log.exception("liked-songs LocalTrack sync failed (continuing)")
    set_config("spotify_liked_sync_last_json", json.dumps(stats))
    log.info("sync_spotify_liked: new=%d updated=%d removed=%d pages=%d errors=%d",
             stats["new"], stats["updated"], stats["removed"],
             stats["pages_pulled"], len(stats["errors"]))
    return stats



# ====================================================================
# Source enumeration
# ====================================================================
def _sync_liked_songs_local_tracks(stats: dict) -> None:
    """Mirror SpotifyLikedTrack into LocalTrack rows for the sentinel pair.

    Position = 0-based rank in added_at descending order (0 = most-recently-liked,
    matches Spotify's own UI ordering). Tracks no longer in the cache get their
    LocalTrack rows deleted.

    Called from sync_spotify_liked_tracks_tick AFTER a complete cache pull
    (aborted_partial=False). Idempotent.
    """
    from .db import PlaylistPair, SpotifyLikedTrack, LocalTrack
    with session_scope() as s:
        pair = s.scalar(
            select(PlaylistPair).where(
                PlaylistPair.spotify_playlist_id == LIKED_SONGS_SENTINEL
            )
        )
        if not pair:
            log.debug("liked-songs LocalTrack sync: no sentinel pair (bootstrap pending)")
            return
        liked = list(s.scalars(
            select(SpotifyLikedTrack).order_by(
                SpotifyLikedTrack.added_at_spotify.desc().nulls_last()
            )
        ).all())
        existing_by_id = {
            lt.spotify_track_id: lt for lt in s.scalars(
                select(LocalTrack).where(LocalTrack.pair_id == pair.id)
            ).all()
        }
        seen_ids: set[str] = set()
        added, updated, removed = 0, 0, 0
        for pos, slt in enumerate(liked):
            seen_ids.add(slt.spotify_track_id)
            existing = existing_by_id.get(slt.spotify_track_id)
            if existing:
                changed = False
                if existing.position != pos:
                    existing.position = pos; changed = True
                if existing.artist != slt.artist:
                    existing.artist = slt.artist; changed = True
                if existing.album != slt.album:
                    existing.album = slt.album; changed = True
                if existing.title != slt.title:
                    existing.title = slt.title; changed = True
                if changed:
                    updated += 1
            else:
                s.add(LocalTrack(
                    pair_id=pair.id,
                    spotify_track_id=slt.spotify_track_id,
                    artist=slt.artist,
                    album=slt.album,
                    title=slt.title,
                    isrc=slt.isrc,
                    position=pos,
                    added_at=slt.added_at_spotify,
                ))
                added += 1
        for tid, lt in existing_by_id.items():
            if tid not in seen_ids:
                s.delete(lt); removed += 1
        stats["liked_localtracks_total"] = len(liked)
        stats["liked_localtracks_added"] = added
        stats["liked_localtracks_updated"] = updated
        stats["liked_localtracks_removed"] = removed
    log.info("liked-songs LocalTrack sync: total=%d added=%d updated=%d removed=%d",
             len(liked), added, updated, removed)


def _read_liked_songs(limit: Optional[int] = None) -> list[tuple[str, str, str, str, Optional[str]]]:
    """Return [(spotify_track_id, artist, album, title, isrc), ...] from the
    LOCAL CACHE (SpotifyLikedTrack table).

    The cache is refreshed by sync_spotify_liked_tracks_tick on a 6h schedule
    (or on demand via /library-autofill/sync-spotify-now). This function NEVER
    calls Spotify directly, so the engine survives 429 rate limiting.

    First-boot bootstrap: if the cache is empty, do one synchronous sync.
    """
    from .db import SpotifyLikedTrack
    out: list[tuple[str, str, str, str, Optional[str]]] = []
    with session_scope() as s:
        q = select(SpotifyLikedTrack).order_by(SpotifyLikedTrack.added_at_spotify.desc())
        if limit:
            q = q.limit(limit)
        rows = list(s.scalars(q).all())
        for r in rows:
            out.append((r.spotify_track_id, r.artist, r.album, r.title, r.isrc))
    # First-boot: cache empty → do a synchronous sync (one-time bootstrap)
    if not out:
        log.info("_read_liked_songs: cache empty — running first-boot bootstrap sync")
        try:
            sync_spotify_liked_tracks_tick()
        except Exception:
            log.exception("first-boot sync failed")
        # Re-read after sync
        with session_scope() as s:
            q = select(SpotifyLikedTrack).order_by(SpotifyLikedTrack.added_at_spotify.desc())
            if limit:
                q = q.limit(limit)
            for r in s.scalars(q).all():
                out.append((r.spotify_track_id, r.artist, r.album, r.title, r.isrc))
    log.debug("_read_liked_songs: returning %d rows from local cache", len(out))
    return out



def _read_playlist_tracks(playlist_id: str) -> list[tuple[str, str, str, str, Optional[str]]]:
    raw = spotify_client.get_playlist_tracks(playlist_id) or []
    return [(tr.id, tr.artist, tr.album or "", tr.title, tr.isrc) for tr in raw]


def _read_followed_artists_albums() -> list[tuple[str, str, str, str, Optional[str]]]:
    """The 'Followed Artists' feed: every album + single by every artist the
    user follows on Spotify. Same album_type filters and synthetic album-id
    rows as the discography expander, so the rest of the pipeline (dedup,
    queueing, canonical-artist resolution) just works. High volume by design —
    the existing per-album dedup keeps re-scans cheap."""
    from . import auth_spotify, spotify_client
    sp = auth_spotify.get_client()
    if not sp:
        raise RuntimeError("Spotify not connected")
    artists: dict[str, str] = {}
    after = None
    while True:
        page = spotify_client._retry_patient(sp.current_user_followed_artists, limit=50, after=after)
        block = (page or {}).get("artists") or {}
        items = block.get("items") or []
        for a in items:
            if a.get("id"):
                artists[a["id"]] = a.get("name") or ""
        after = (block.get("cursors") or {}).get("after")
        if not after or not items or len(artists) >= 500:
            break
    log.info("followed_artists: %d artists followed", len(artists))
    out: list[tuple] = []
    seen_album_ids: set[str] = set()
    for aid, aname in artists.items():
        try:
            offset = 0
            while True:
                page = spotify_client._retry_patient(
                    sp.artist_albums, aid,
                    album_type="album,single", limit=50, offset=offset,
                )
                items = (page or {}).get("items") or []
                if not items:
                    break
                for alb in items:
                    al_id = alb.get("id")
                    if not al_id or al_id in seen_album_ids:
                        continue
                    seen_album_ids.add(al_id)
                    out.append((al_id, aname, alb.get("name") or "",
                                alb.get("name") or "", None))
                if len(items) < 50:
                    break
                offset += 50
                if offset >= 200:  # cap per artist, same as discography mode
                    break
        except Exception:
            log.exception("followed_artists: artist_albums for %s failed", aname)
    log.info("followed_artists: %d album rows generated", len(out))
    return out



def gather_source_tracks(sources: list[str]) -> tuple[list[tuple], list[str]]:
    """Combine all enabled sources into one big list, deduped by track id.

    B10: returns (tracks, errors) instead of swallowing errors silently,
    so the UI can show what went wrong.
    """
    tracks: list[tuple] = []
    seen_track_ids: set[str] = set()
    errors: list[str] = []
    for s in sources:
        try:
            if s == "liked":
                rows = _read_liked_songs()
            elif s.startswith("playlist:"):
                rows = _read_playlist_tracks(s.split(":", 1)[1])
            elif s == "followed_artists":
                rows = _read_followed_artists_albums()
            else:
                errors.append(f"unknown source '{s}'")
                continue
            for row in rows:
                if row[0] not in seen_track_ids:
                    seen_track_ids.add(row[0])
                    tracks.append(row)
        except Exception as e:
            # B10/B18: log full traceback AND surface to caller
            log.exception("autofill: failed reading source %s", s)
            errors.append(f"source '{s}': {type(e).__name__}: {e}")
    return tracks, errors


# ====================================================================
# Discography expander — for 'discography' acquisition mode.
# For each unique artist with liked tracks, fetches every album the
# artist has released via Spotify API + returns (artist, album, song)
# tuples so the rest of the pipeline can queue each.
# ====================================================================
def expand_artists_to_albums(tracks: list[tuple]) -> list[tuple]:
    """Replace per-track entries with per-album entries covering each artist's
    full discography. Returns the SAME shape as gather_source_tracks() output:
    [(spotify_track_id, artist, album, title, isrc), ...]

    For each unique artist seen in input, fetches Spotify's artist_albums
    (album, single — NOT compilation/appears_on, to avoid features). Each
    album becomes a synthetic row with the album_id-derived spotify_track_id
    (so the dedup-by-track-id machinery still works) + artist + album name.
    """
    from . import auth_spotify, spotify_client
    sp = auth_spotify.get_client()
    if not sp:
        log.warning("discography expand: Spotify not authed; returning unchanged")
        return tracks

    # Step 1: from input tracks, extract unique artists + try to map to spotify artist IDs.
    # Spotify track has primary artist info, but we only have artist NAME in the tuple.
    # We need the artist's Spotify ID. Pull them by re-fetching the track.
    track_ids = {t[0] for t in tracks if t[0]}
    artist_ids: dict[str, str] = {}  # artist_id -> artist_name (canonical from Spotify)
    sample_track_per_artist: dict[str, tuple] = {}  # artist_id -> sample (id, artist, album, title, isrc)
    for tid in list(track_ids)[:200]:  # safety cap: don't fetch more than 200 tracks
        try:
            t = spotify_client._retry_patient(sp.track, tid)
        except Exception:
            continue
        if not t:
            continue
        primary = (t.get("artists") or [{}])[0]
        aid = primary.get("id")
        aname = primary.get("name")
        if not aid:
            continue
        artist_ids[aid] = aname
        if aid not in sample_track_per_artist:
            sample_track_per_artist[aid] = (
                tid, aname, (t.get("album") or {}).get("name") or "",
                t.get("name") or "", (t.get("external_ids") or {}).get("isrc"),
            )

    log.info("discography expand: %d unique artists to fetch", len(artist_ids))

    # Step 2: for each artist, fetch all album_groups in {album, single}
    out: list[tuple] = []
    seen_album_ids: set[str] = set()
    for aid, aname in artist_ids.items():
        try:
            offset = 0
            while True:
                page = spotify_client._retry_patient(
                    sp.artist_albums, aid,
                    album_type="album,single", limit=50, offset=offset,
                )
                items = (page or {}).get("items") or []
                if not items:
                    break
                for alb in items:
                    aid_alb = alb.get("id")
                    if not aid_alb or aid_alb in seen_album_ids:
                        continue
                    seen_album_ids.add(aid_alb)
                    out.append((
                        aid_alb,                             # synthetic "track id" = album id
                        aname,                               # artist name
                        alb.get("name") or "",               # album name
                        alb.get("name") or "",               # title (used as sample song)
                        None,                                # ISRC unknown at album level
                    ))
                if len(items) < 50:
                    break
                offset += 50
                if offset >= 200:  # cap at 200 albums per artist
                    break
        except Exception:
            log.exception("discography expand: artist_albums for %s failed", aname)
    log.info("discography expand: %d total album rows generated", len(out))
    return out


# ====================================================================
# Coverage check — what's already on disk (B1)
# ====================================================================
def _plex_known_track_ids(track_ids: Iterable[str]) -> set[str]:
    """Spotify track IDs that have a Plex match in TrackMapping."""
    ids = list(track_ids)
    if not ids:
        return set()
    found: set[str] = set()
    with session_scope() as s:
        chunk = 500
        for i in range(0, len(ids), chunk):
            page = ids[i:i + chunk]
            rows = s.execute(
                select(TrackMapping.spotify_track_id)
                .where(TrackMapping.spotify_track_id.in_(page))
                .where(TrackMapping.plex_track_key.isnot(None))
            ).all()
            for (tid,) in rows:
                found.add(tid)
    return found


def _plex_match_coverage_pct(total_seen: int) -> int:
    """% of the user's local_tracks that have a Plex match.

    Surfaced in the UI as "Plex coverage: X%" — if low, the autofill will
    over-recommend because most tracks look 'missing' just because the Plex
    matcher hasn't scanned them yet.
    """
    if total_seen <= 0:
        return 0
    with session_scope() as s:
        matched = s.query(TrackMapping).filter(TrackMapping.plex_track_key.isnot(None)).count()
        total_mappings = s.query(TrackMapping).count()
    if total_mappings == 0:
        return 0
    return int(100 * matched / total_mappings)


# ====================================================================
def _group_albums(tracks):
    groups: dict[tuple[str, str], dict] = {}
    for tid, artist, album, title, isrc in tracks:
        if not artist or not album:
            continue
        # B5: store BOTH normalized key for dedup AND original for Lidarr lookup
        key = (artist.strip(), album.strip())
        g = groups.setdefault(key, {"track_ids": set(), "titles": [], "isrcs": set()})
        g["track_ids"].add(tid)
        g["titles"].append(title)
        if isrc:
            g["isrcs"].add(isrc)
    return groups


# ====================================================================
# Action recording (B5, B17, B20)
# ====================================================================
def _record_action_in_session(s, *, artist: str, album: str, status: str,
                              foreign_album_id: Optional[str] = None,
                              note: str = "",
                              track_ids: Optional[set[str]] = None,
                              pre_existing_files: Optional[int] = None) -> None:
    """B17: caller owns the session — no per-row open/close.
    B20: no explicit commit — session_scope handles it.
    B5: lookup by normalized key, store both raw + normalized in the row.
    """
    artist_key = _normalize_for_key(artist)
    album_key = _normalize_for_key(album)
    existing = s.scalar(
        select(AutofillAction)
        .where(AutofillAction.artist_key == artist_key)
        .where(AutofillAction.album_key == album_key)
    )
    ids_json = json.dumps(sorted(track_ids or []))
    if existing:
        # B21: only bump attempt_count when STATUS actually changes or this is a
        # real retry. Steady-state re-confirms (queued -> queued) aren't attempts.
        real_attempt = existing.status != status or status not in ("queued", "imported", "downloading")
        existing.status = status
        existing.note = note[:1024]
        if foreign_album_id:
            existing.foreign_album_id = foreign_album_id
        existing.track_ids_json = ids_json
        existing.last_attempt_at = datetime.utcnow()
        if real_attempt:
            existing.attempt_count = (existing.attempt_count or 0) + 1
    else:
        from sqlalchemy.exc import IntegrityError as _IntegrityError
        try:
            with s.begin_nested():
                s.add(AutofillAction(
                    artist=artist, album=album,
                    artist_key=artist_key, album_key=album_key,
                    status=status,
                    foreign_album_id=foreign_album_id,
                    note=note[:1024], track_ids_json=ids_json,
                    last_attempt_at=datetime.utcnow(),
                    attempt_count=1,
                    pre_existing_files=pre_existing_files,
                ))
        except _IntegrityError:
            row2 = s.query(AutofillAction).filter_by(
                artist_key=artist_key, album_key=album_key).first()
            if row2:
                row2.status = status
                row2.note = note[:1024]
                if foreign_album_id:
                    row2.foreign_album_id = foreign_album_id
                row2.track_ids_json = ids_json
                row2.last_attempt_at = datetime.utcnow()


# ====================================================================
# The tick
# ====================================================================
def autofill_tick(max_new_albums: int = None) -> dict:  # B11: now config-driven
    stats = {
        "enabled": False, "scanned_tracks": 0, "missing_from_plex": 0,
        "unique_albums": 0, "already_in_lidarr": 0,
        "added_to_lidarr": 0, "lookup_failed": 0, "errors": 0,
        "source_errors": [],
        "plex_coverage_pct": 0,
        "started_at": datetime.utcnow().isoformat() + "Z",
    }
    if (get_config("autofill_enabled", "0") or "0") != "1":
        return stats
    stats["enabled"] = True

    # B11: per-tick cap is dynamic via AppConfig so users can tune via UI.
    # SERIALIZATION fix: default to 1 (one album in flight at a time).
    # Power users can override via AppConfig autofill_max_new_per_tick.
    if max_new_albums is None:
        try:
            max_new_albums = int(get_config("autofill_max_new_per_tick", "1") or 1)
        except (ValueError, TypeError):
            max_new_albums = 1

    # SERIALIZATION fix: if anything is already in flight downstream, defer.
    serialize = (get_config("autofill_serialize", "1") or "1") == "1"
    if serialize:
        busy, reason = is_pipeline_busy()
        if busy:
            # A concurrency back-off is NORMAL operation — do NOT record it as a (failed)
            # run, or the Status card shows "Last run failed" + "Source errors: pipeline
            # busy" and the previous good run record gets overwritten.
            log.info("autofill_tick: deferring — pipeline busy: %s", reason)
            stats["enabled"] = True
            stats["deferred"] = reason
            return stats

    sources_json = get_config("autofill_sources_json") or json.dumps(DEFAULT_SOURCES)
    try:
        sources = json.loads(sources_json)
    except (ValueError, TypeError):  # B18
        sources = DEFAULT_SOURCES

    if not sources:
        return stats

    if not TICK_LOCK.acquire(blocking=False):
        log.info("autofill_tick: overlapping tick, skipping")
        return stats
    try:
        tracks, source_errors = gather_source_tracks(sources)  # B10
        stats["scanned_tracks"] = len(tracks)
        stats["source_errors"] = source_errors
        if source_errors:
            stats["errors"] += len(source_errors)

        # Acquisition mode: song | album | discography (default 'album')
        mode = (get_config("autofill_acquisition_mode", "album") or "album").lower()
        stats["mode"] = mode
        if mode == "discography" and tracks:
            stats["scanned_tracks_pre_discog"] = len(tracks)
            tracks = expand_artists_to_albums(tracks)
            stats["scanned_tracks"] = len(tracks)
            stats["source_errors"].append(
                f"discography mode: expanded to {len(tracks)} albums "
                f'from artists in {stats["scanned_tracks_pre_discog"]} liked tracks'
            )

        if not tracks:
            # AUDIT-1: preserve previous valid coverage instead of clobbering with 0
            try:
                _prev = json.loads(get_config("autofill_last_successful_run_json") or get_config("autofill_last_run_json") or "{}")
                _pcv = _prev.get("plex_coverage_pct")
                if isinstance(_pcv, (int, float)) and _pcv > 0:
                    stats["plex_coverage_pct"] = _pcv
            except Exception:
                pass
            stats["finished_at"] = datetime.utcnow().isoformat() + "Z"
            _persist_run(stats)
            return stats

        stats["plex_coverage_pct"] = _plex_match_coverage_pct(len(tracks))  # B1

        plex_known = _plex_known_track_ids([t[0] for t in tracks])
        missing = [t for t in tracks if t[0] not in plex_known]
        stats["missing_from_plex"] = len(missing)

        groups = _group_albums(missing)
        stats["unique_albums"] = len(groups)

        # SF7/SF8: Queue albums for SpotiFLAC picker_tick to acquire.
        # No Lidarr lookup needed — picker_tick resolves Spotify URLs directly.
        from sqlalchemy import select as _select
        with session_scope() as _s:
            existing_keys = {
                (row[0], row[1]) for row in _s.execute(
                    _select(AutofillAction.artist_key, AutofillAction.album_key)
                ).all()
            }
        new_count = 0
        group_items = list(groups.items())
        def _sort_key(kv):
            artist, album = kv[0]
            ak, bk = _normalize_for_key(artist), _normalize_for_key(album)
            return (1 if (ak, bk) in existing_keys else 0)
        group_items.sort(key=_sort_key)
        with session_scope() as s:
            for (artist, album), info in group_items:
                if new_count >= max_new_albums:
                    log.info("autofill: max_new_albums=%d, deferring rest", max_new_albums)
                    break
                artist_key = _normalize_for_key(artist)
                album_key = _normalize_for_key(album)
                existing = s.scalar(
                    select(AutofillAction)
                    .where(AutofillAction.artist_key == artist_key)
                    .where(AutofillAction.album_key == album_key)
                )
                if existing:
                    if (existing.attempt_count or 0) >= MAX_ATTEMPTS \
                            and existing.status in ABANDONED_STATUSES:
                        existing.status = "abandoned"
                        continue
                    if existing.status == "pending_retry":
                        pass
                    elif existing.status in ABANDONED_STATUSES:
                        # L-5: last_attempt_at can be NULL; treat as retry-eligible.
                        if existing.last_attempt_at is not None:
                            ago = datetime.utcnow() - existing.last_attempt_at
                            if ago < timedelta(hours=RETRY_AFTER_HOURS):
                                continue
                    elif existing.status in ("queued", "downloading", "imported", "library_existing"):
                        stats["already_in_lidarr"] += 1
                        continue
                    elif existing.status == "abandoned":
                        continue
                _record_action_in_session(
                    s, artist=artist, album=album, status="queued",
                    note="queued for spotiflac",
                    track_ids=info["track_ids"],
                    pre_existing_files=0,
                )
                stats["added_to_lidarr"] += 1
                new_count += 1
        stats["new_queued"] = new_count

        stats["finished_at"] = datetime.utcnow().isoformat() + "Z"
        _persist_run(stats)
        return stats
    finally:
        TICK_LOCK.release()


# ====================================================================
# ====================================================================
# Reconciler — lightweight count of queued/imported rows (SF7+).
# Lidarr/slskd removed; SpotiFLAC picker_tick handles status updates.
# ====================================================================
def reconcile_tick() -> dict:
    """Light reconcile: surfaces counts for the dashboard.
    SpotiFLAC picker_tick handles actual status updates.
    """
    out = {"checked": 0, "downloading": 0, "imported": 0, "failed": 0,
           "still_queued": 0, "pipeline_reachable": True,
           "started_at": datetime.utcnow().isoformat() + "Z"}
    with session_scope() as s:
        rows = list(s.scalars(
            select(AutofillAction)
            .where(AutofillAction.status.in_(["queued", "downloading", "imported"]))
        ).all())
        for r in rows:
            out["checked"] += 1
            if r.status == "imported":
                out["imported"] += 1
            elif r.status == "downloading":
                out["downloading"] += 1
            else:
                out["still_queued"] += 1
    out["finished_at"] = datetime.utcnow().isoformat() + "Z"
    set_config("autofill_last_reconcile_json", json.dumps(out))
    return out


def _build_disk_index():
    """(lc_top, album_dirs) for disk-truth checks that MIRROR placement.
    lc_top: lower(folder) -> actual top-level artist folder (case-insensitive resolve).
    album_dirs: norm(album-subdir) -> {artist folders containing it} so a file stored
    under the canonical album-artist or 'Various Artists' is still found when that differs
    from the liked track's raw performer. Built once per tick (one shallow two-level walk)."""
    import os as _os, re as _re
    lc_top = {}
    album_dirs = {}
    try:
        tops = _os.listdir(_LOCAL_PATH_PREFIX)
    except OSError:
        tops = []
    for t in tops:
        tp = _os.path.join(_LOCAL_PATH_PREFIX, t)
        if not _os.path.isdir(tp):
            continue
        lc_top.setdefault(t.lower(), t)
        try:
            subs = _os.listdir(tp)
        except OSError:
            subs = []
        for sub in subs:
            if _os.path.isdir(_os.path.join(tp, sub)):
                album_dirs.setdefault(_re.sub(r"[^a-z0-9]", "", sub.lower()), set()).add(t)
    return lc_top, album_dirs


def _song_on_disk(title, artist, album, idx, cache=None):
    """True if an audio file whose title-key matches `title` exists either under the
    case-insensitively resolved artist folder OR under wherever `album` actually lives on
    disk -- covering combined-credit / compilation / soundtrack / casing variants where the
    placement folder differs from the raw performer. idx = _build_disk_index() output;
    cache is a per-tick dict so each folder is scanned at most once."""
    import os as _os, re as _re
    AUD = (".flac", ".mp3", ".m4a", ".alac", ".aac", ".ogg", ".opus", ".wav")
    lc_top, album_dirs = idx
    if cache is None:
        cache = {}
    want = _norm_title_key(title or "")
    if not want:
        return True

    def _hit(keys):
        return any(k and (k == want or want in k or k in want) for k in keys)

    # 1) artist folder, resolved case-insensitively (recursive -- any album under it)
    a = _safe_for_fs(artist or "")
    ck = ("A", a.lower())
    if ck not in cache:
        keys = set()
        real = lc_top.get(a.lower())
        if real:
            for root, _d, files in _os.walk(_os.path.join(_LOCAL_PATH_PREFIX, real)):
                for fn in files:
                    if fn.lower().endswith(AUD):
                        kk = _file_title_key(_os.path.join(root, fn), artist or "")
                        if kk:
                            keys.add(kk)
        cache[ck] = keys
    if _hit(cache[ck]):
        return True

    # 2) wherever the album physically lives (album-artist / Various Artists / variant)
    na = _re.sub(r"[^a-z0-9]", "", (album or "").lower())
    an = _re.sub(r"[^a-z0-9]", "", (artist or "").lower())
    if na:
        ck2 = ("B", na, an)
        if ck2 not in cache:
            keys = set()
            cand = set(album_dirs.get(na, ()))
            if not cand:
                # Containment (either direction) bridges edition suffixes like
                # 'Interstellar (...) [Expanded Edition]' -> folder 'Interstellar'.
                # The VA/credit scoping below is what keeps this safe, so a length
                # bound is unnecessary; require >=5 chars to skip trivial substrings.
                for k, v in album_dirs.items():
                    if k and min(len(na), len(k)) >= 5 and (na in k or k in na):
                        cand |= v
            # Only trust an album folder that plausibly belongs to THIS track: the
            # canonical album-artist of a feature/collab contains the performer
            # ('Sam Smith' -> 'Sam Smith, Kim Petras'), or it is a compilation under
            # 'Various Artists'. Stops a generic album name ('Spotify Singles',
            # 'Greatest Hits') from matching an unrelated artist's folder (false-present).
            arts = set()
            for art in cand:
                fa = _re.sub(r"[^a-z0-9]", "", art.lower())
                if fa == "variousartists" or (an and (an in fa or fa in an)):
                    arts.add(art)
            for art in arts:
                base = _os.path.join(_LOCAL_PATH_PREFIX, art)
                try:
                    subs = _os.listdir(base)
                except OSError:
                    subs = []
                for sub in subs:
                    sp = _os.path.join(base, sub)
                    if not _os.path.isdir(sp):
                        continue
                    ns = _re.sub(r"[^a-z0-9]", "", sub.lower())
                    if not (ns == na or na in ns or ns in na):
                        continue
                    try:
                        fns = _os.listdir(sp)
                    except OSError:
                        fns = []
                    for fn in fns:
                        if fn.lower().endswith(AUD):
                            kk = _file_title_key(_os.path.join(sp, fn))
                            if kk:
                                keys.add(kk)
            cache[ck2] = keys
        if _hit(cache[ck2]):
            return True
    return False


def requeue_missing_tick(batch: int = 150) -> dict:
    """Self-healing disk-truth reconcile — the standing counterpart to
    mark_present_queued_tick. Walks a rotating slice of the rows the engine THINKS it
    already has ('library_existing' / 'imported') and re-queues the ones whose songs
    are genuinely NOT on disk: false 'already in Lidarr at add time' markers, plus files
    later removed or atticed (e.g. by the live-ban). Covers LIKED songs (per-artist
    title-key disk check) and FILLER (an imported row whose every file is gone). Partial
    albums are left to complete_albums_tick; complete_locked is never touched. Each flip
    is ledgered to /data/requeue_ledger.jsonl for rollback. The rotating cursor keeps the
    NAS folder-walk cheap, and it runs on a schedule so coverage self-corrects over time
    instead of needing a manual re-scan."""
    import json as _json, time as _time
    from .db import AutofillAction, SpotifyLikedTrack, TrackMapping
    out = {"checked": 0, "requeued": 0, "files_gone": 0, "liked_absent": 0,
           "started_at": datetime.utcnow().isoformat() + "Z"}
    new_cursor = None
    try:
        with session_scope() as s:
            try:
                cursor = int(get_config("requeue_scan_cursor", "0") or 0)
            except Exception:
                cursor = 0
            rows = (s.query(AutofillAction)
                    .filter(AutofillAction.status.in_(("library_existing", "imported")),
                            AutofillAction.id > cursor)
                    .order_by(AutofillAction.id).limit(batch).all())
            if not rows and cursor:                       # wrapped past the end -> restart
                cursor = 0
                rows = (s.query(AutofillAction)
                        .filter(AutofillAction.status.in_(("library_existing", "imported")))
                        .order_by(AutofillAction.id).limit(batch).all())
            if rows:
                liked = {lt.spotify_track_id: lt for lt in s.query(SpotifyLikedTrack).all()}
                mapped = {m.spotify_track_id for m in
                          s.query(TrackMapping).filter(TrackMapping.plex_track_key.isnot(None)).all()}
                # Disk-truth detection MUST mirror placement: files are stored under the
                # canonical album-artist / 'Various Artists' folder (case-canonicalized),
                # NOT the raw per-track performer. Resolve folders case-insensitively and
                # fall back to wherever the album actually lives -- else combined-credit /
                # compilation / soundtrack / casing variants churn forever as false-missing.
                _disk_idx = _build_disk_index()
                _disk_cache = {}

                def _on_disk(lt, album=""):
                    return _song_on_disk(lt.title, lt.artist, album, _disk_idx, _disk_cache)

                led = open("/data/requeue_ledger.jsonl", "a")
                last_id = cursor
                for r in rows:
                    last_id = r.id
                    out["checked"] += 1
                    try:
                        try:
                            tids = _json.loads(r.track_ids_json or "[]")
                        except Exception:
                            tids = []
                        unc = [liked[t] for t in tids if t in liked and t not in mapped]
                        _alb = getattr(r, "album", "") or ""
                        liked_missing = [lt for lt in unc if not _on_disk(lt, _alb)]
                        try:
                            paths = _json.loads(r.imported_paths or "[]")
                        except Exception:
                            paths = []
                        # Historical rows store the OLD music root from before the
                        # folder rename; re-root to the live prefix before the existence check,
                        # and skip paths under an unrecognized root (inconclusive -> never flag),
                        # so a stale prefix can never masquerade as "every file gone".
                        chk = []
                        for p in paths:
                            for _old in (_LOCAL_PATH_PREFIX + "/",):
                                if p.startswith(_old):
                                    chk.append(_LOCAL_PATH_PREFIX + "/" + p[len(_old):]); break
                        # imported_paths go stale whenever the rulebook relocates files, so an
                        # existence check produces false "all gone" hits. Disabled — re-queueing now
                        # relies solely on the relocation-proof liked-song disk-truth check above.
                        files_gone = False
                        if not (liked_missing or files_gone):
                            continue
                        old = r.status
                        r.status = "queued"
                        why = []
                        if files_gone:
                            why.append("%d imported file(s) gone" % len(paths)); out["files_gone"] += 1
                        if liked_missing:
                            why.append("%d liked song(s) absent from disk" % len(liked_missing)); out["liked_absent"] += 1
                        r.note = ("requeue(auto): was %s; %s" % (old, "; ".join(why)))[:1024]
                        led.write(_json.dumps({"row_id": r.id, "old_status": old,
                                               "files_gone": files_gone,
                                               "liked_absent": len(liked_missing),
                                               "ts": _time.time(), "tick": "requeue_missing_tick"}) + "\n")
                        out["requeued"] += 1
                    except Exception:
                        log.exception("requeue_missing_tick: row %s failed", getattr(r, "id", "?"))
                led.close()
                new_cursor = last_id if len(rows) == batch else 0
            else:
                new_cursor = 0
    except Exception as e:
        out["error"] = str(e)[:200]
        log.exception("requeue_missing_tick failed")
    if new_cursor is not None:
        try:
            set_config("requeue_scan_cursor", str(new_cursor))
        except Exception:
            pass
    out["finished_at"] = datetime.utcnow().isoformat() + "Z"
    try:
        set_config("requeue_scan_last_json", json.dumps(out))
    except Exception:
        pass
    return out


def trigger_immediate_tick() -> None:
    t = threading.Thread(target=autofill_tick, name="autofill-immediate", daemon=True)
    t.start()



# ====================================================================
# Mirror trigger -- B2: after SpotiFLAC imports, invalidate negative Plex cache
# and kick sync_engine.mirror_from_local for all pairs containing the new tracks.
# ====================================================================
def _trigger_mirror_for_imported_album(autofill_row) -> int:
    """Returns # of mirror runs triggered."""
    if not autofill_row.track_ids_json:
        return 0
    try:
        spotify_track_ids = set(json.loads(autofill_row.track_ids_json) or [])
    except Exception:
        return 0
    if not spotify_track_ids:
        return 0

    from . import sync_engine
    from .db import LocalTrack, TrackMapping

    triggered = 0
    with session_scope() as s:
        pair_ids = set(
            r[0] for r in s.execute(
                select(LocalTrack.pair_id).where(LocalTrack.spotify_track_id.in_(spotify_track_ids))
            ).all()
        )
        if not pair_ids:
            return 0
        # B7: clear negative cache so matcher re-searches Plex for new FLACs
        s.execute(
            TrackMapping.__table__.update()
            .where(TrackMapping.spotify_track_id.in_(spotify_track_ids))
            .where(TrackMapping.plex_track_key.is_(None))
            .values(plex_searched_at=None)
        )

    for pid in pair_ids:
        try:
            import threading as _th
            _th.Thread(
                target=sync_engine.mirror_from_local,
                kwargs={"pair_id": pid, "include_deletions": False},
                daemon=True,
            ).start()
            triggered += 1
        except Exception:
            pass
    return triggered


# ====================================================================
_BIGGEST_ALBUM_CACHE: dict[str, tuple[str, int, str, list[str], float]] = {}
# track_id -> (album_id, total_tracks, album_name, album_track_ids, ts)
_BIGGEST_ALBUM_CACHE_TTL_S = 7 * 86400  # 7 days
_BIGGEST_ALBUM_CACHE_LOCK = threading.Lock()




def mark_present_queued_tick(batch: int = 600) -> dict:
    """Stop needless re-attempts: mark queued rows whose album is ALREADY on disk (it was
    downloaded under a different artist-credit, or by an earlier run) as 'library_existing'
    so the picker skips them. Safe — for rows with track ids, every requested track must
    actually be present in the folder by title before we skip; album rows with no track
    ids are skipped only when their canonical folder already holds files."""
    import os as _os, json as _json, re as _re
    from .db import SessionLocal, AutofillAction, SpotifyLikedTrack
    out = {"checked": 0, "marked": 0}

    def _cf(name):
        n = _re.sub(r'^\s*\d{1,3}\s*[-._)]?\s+', '', (name or ''))
        return _re.sub(r'[^a-z0-9]', '', n.lower())

    AUDIO = (".flac", ".m4a", ".mp3", ".alac", ".aac", ".ogg", ".opus", ".wav")
    try:
        with SessionLocal() as s:
            rows = s.query(AutofillAction).filter(AutofillAction.status == "queued").limit(batch).all()
            for r in rows:
                out["checked"] += 1
                try:
                    tids = _json.loads(r.track_ids_json or "[]")
                except Exception:
                    tids = []
                aa, _ = _canonical_album_artist(r.artist, r.album, tids, r.foreign_album_id)
                folder = "/Volumes/MediaVolume3/plexify-music/%s/%s" % (_safe_for_fs(aa, "Unknown Artist"),
                                            _safe_for_fs(r.album, "Unknown Album"))
                try:
                    files = [f for f in _os.listdir(folder)
                             if not f.startswith('.') and f.lower().endswith(AUDIO)] if _os.path.isdir(folder) else []
                except Exception:
                    files = []
                if not files:
                    continue
                file_keys = {_cf(_os.path.splitext(f)[0]) for f in files}
                present = True
                if tids:
                    for t in tids:
                        lt = s.get(SpotifyLikedTrack, t)
                        title = lt.title if lt else None
                        tk = _cf(title) if title else None
                        if not tk or not any(tk and (tk in fk or fk in tk) for fk in file_keys):
                            present = False
                            break
                if present:
                    r.status = "library_existing"
                    r.note = ((r.note or "")[:400] + " | present on disk — skipped re-download")
                    out["marked"] += 1
            s.commit()
    except Exception:
        log.exception("mark_present_queued_tick failed")
    log.info("mark_present_queued: %s", out)
    return out


_DISPUTES_PATH = "/data/disputed_sources.json"


def _load_disputes() -> dict:
    try:
        return json.load(open(_DISPUTES_PATH))
    except Exception:
        return {}


def _disputed_sources(track_ids) -> set:
    """Sources the user has disputed for ANY of these tracks (so the picker skips
    them — \"don't download from wherever it got that wrong song from\")."""
    d = _load_disputes()
    out = set()
    for t in (track_ids or []):
        for src in d.get(t, []) or []:
            if src:
                out.add(src)
    return out


def add_dispute(track_id: str, source: str) -> None:
    d = _load_disputes()
    lst = d.setdefault(track_id, [])
    if source and source not in lst:
        lst.append(source)
    try:
        json.dump(d, open(_DISPUTES_PATH, "w"))
    except Exception:
        log.exception("add_dispute: could not persist")


def dispute_file(row_id, file_path) -> dict:
    """Dispute a specific album track by file (e.g. a non-liked filler track that's the
    wrong audio). Attic the file, blacklist the album's source for the liked songs it
    covers (so re-acquire avoids it), and re-queue the album."""
    import os as _os, json as _json, time as _time, shutil as _sh
    from .db import SessionLocal, AutofillAction
    out = {"ok": False}
    with SessionLocal() as s:
        try:
            row = s.get(AutofillAction, int(row_id))
        except Exception:
            row = None
        if not row:
            out["error"] = "row not found"
            return out
        src = ((row.source) or "unknown").split(":")[0]
        p = file_path or ""
        for _o in ("/plexify-music/",):
            if p.startswith(_o):
                p = "/Volumes/MediaVolume3/plexify-music/" + p[len(_o):]
                break
        atticed = 0
        if p.startswith("/Volumes/MediaVolume3/plexify-music/") and _os.path.isfile(p):
            dst = "/Volumes/MediaVolume3/Downloads/music/_attic_dupes/disputed/" + p[len("/Volumes/MediaVolume3/plexify-music/"):]
            try:
                _os.makedirs(_os.path.dirname(dst), exist_ok=True)
                _sh.move(p, dst)
                _log_recovery_move("dispute", p, dst)
                atticed = 1
            except Exception:
                log.exception("dispute_file: attic failed for %s", p)
        try:
            tids = _json.loads(row.track_ids_json or "[]")
        except Exception:
            tids = []
        for t in tids:
            add_dispute(t, src)
        try:
            open("/data/disputes.jsonl", "a").write(_json.dumps(
                {"ts": _time.time(), "row_id": int(row_id), "file": p, "source": src, "kind": "file"}) + "\n")
        except Exception:
            pass
        if row.status != "queued":
            row.status = "queued"
            row.note = ("disputed track; re-acquire avoiding source '%s'" % src)[:1024]
            row.hires_upgrade_attempted = False
        s.commit()
        out.update(ok=True, source=src, atticed=atticed, row_id=int(row_id))
    return out


def dispute_song(track_id: str) -> dict:
    """User flagged a liked song as the WRONG audio (wrong recording/cover/rendition).
    Attic the file, blacklist the source it came from for this track so the picker
    won't re-download from there, and re-queue so it re-acquires from another source."""
    import os as _os, json as _json, time as _time, shutil as _sh
    from .db import SessionLocal, AutofillAction, SpotifyLikedTrack
    out = {"ok": False}
    with SessionLocal() as s:
        lt = s.get(SpotifyLikedTrack, track_id)
        if not lt:
            out["error"] = "not a liked song"
            return out
        want = _norm_title_key(lt.title or "")
        row = None
        for r in s.query(AutofillAction).filter(
                AutofillAction.status.in_(("imported", "complete_locked", "library_existing"))).all():
            try:
                tids = _json.loads(r.track_ids_json or "[]")
            except Exception:
                tids = []
            if track_id in tids:
                row = r
                break
        src = ((row.source if row else None) or "unknown").split(":")[0]
        atticed = []
        if row:
            try:
                paths = _json.loads(row.imported_paths or "[]")
            except Exception:
                paths = []
            for p in paths:
                for _o in ("/plexify-music/",):
                    if p.startswith(_o):
                        p = "/Volumes/MediaVolume3/plexify-music/" + p[len(_o):]
                        break
                if not _os.path.isfile(p):
                    continue
                k = _file_title_key(p, lt.artist or "")
                if k and (k == want or want in k or k in want):
                    dst = "/Volumes/MediaVolume3/Downloads/music/_attic_dupes/disputed/" + p[len("/Volumes/MediaVolume3/plexify-music/"):]
                    try:
                        _os.makedirs(_os.path.dirname(dst), exist_ok=True)
                        _sh.move(p, dst)
                        _log_recovery_move("dispute", p, dst)
                        atticed.append(p)
                    except Exception:
                        log.exception("dispute_song: attic failed for %s", p)
        if row is None:
            # No imported row links this liked track to a file, so we don't know which file to
            # attic or which source to blacklist. Reporting ok here would make the detector
            # delete the AutoStar row and silently stop watching a still-wrong track without
            # disputing anything — return failure so the row is kept for a later retry instead.
            out["error"] = "liked track not linked to an imported row; nothing to dispute"
            return out
        add_dispute(track_id, src)
        try:
            open("/data/disputes.jsonl", "a").write(_json.dumps(
                {"ts": _time.time(), "track_id": track_id, "title": lt.title,
                 "artist": lt.artist, "source": src, "atticed": atticed,
                 "row_id": (row.id if row else None)}) + "\n")
        except Exception:
            pass
        if row and row.status != "queued":
            row.status = "queued"
            row.note = ("disputed: wrong audio; re-acquire avoiding source '%s'" % src)[:1024]
            row.hires_upgrade_attempted = False
        s.commit()
        out.update(ok=True, source=src, atticed=len(atticed), row_id=(row.id if row else None))
    return out


def picker_tick() -> dict:
    """SpotiFLAC pick — uses spotiflac_adapter.acquire() workflow.

    Picks the OLDEST queued AutofillAction, resolves a Spotify URL for it,
    and hands off to SpotiFLAC for hi-res FLAC download.
    Serialized: one album per tick.
    """
    # Circuit breaker: skip if recent attempts all failed providers.
    from datetime import datetime as _dt_cb
    if _PICKER_COOLDOWN_UNTIL is not None and _dt_cb.utcnow() < _PICKER_COOLDOWN_UNTIL:
        return {"skipped": "circuit_breaker_cooldown",
                "cooldown_until": _PICKER_COOLDOWN_UNTIL.isoformat() + "Z"}
    out = {"checked": 0, "picked": False, "acquired_files": 0, "moved_files": 0,
           "started_at": datetime.utcnow().isoformat() + "Z"}
    _journey_log = []  # phases tried; persisted to row.note for Recent Activity visibility
    # ── 1. Orphan sweep — always runs first, even if picker is disabled.
    # Handles files that arrived via Lidarr or a previous partial run.
    try:
        sweep_res = sweep_orphan_downloads()
        if sweep_res.get("swept", 0) > 0:
            log.info("picker_tick: pre-sweep moved %d files (%d MB) across %d albums",
                     sweep_res["swept"], sweep_res["bytes"] // 1024 // 1024,
                     len(sweep_res.get("by_album", {})))
            out["orphan_sweep"] = sweep_res
    except Exception:
        log.exception("picker_tick: orphan sweep failed (continuing)")

    # ── 1b. Dedup ledger backfill — gently teach the ledger about songs we ALREADY have
    # (cheap: DB rows + filenames, no file reads), a batch at a time, until it's caught up.
    try:
        _bf = _ledger_backfill_step()
        if _bf:
            out["ledger_backfill"] = _bf
    except Exception:
        log.exception("picker_tick: ledger backfill failed (continuing)")

    # ── 2. Gate checks
    if not _ownership_attested():
        out["skipped"] = "legal attestation required — acquisition blocked"
        return out
    if (get_config("autofill_picker_enabled", "0") or "0") != "1":
        out["skipped"] = "picker disabled (autofill_picker_enabled=0)"
        return out
    # Honor serialization
    serialize = (get_config("autofill_serialize", "1") or "1") == "1"
    if serialize:
        busy, reason = is_pipeline_busy()
        if busy:
            out["skipped"] = f"pipeline busy: {reason}"
            return out
    # Movie-priority gate: yield the whole acquisition pass while Plex is streaming
    # video, so high-bitrate playback/transcode isn't starved for CPU/disk.
    if get_config("autofill_pause_when_streaming", "1") == "1":
        _vid = _plex_active_video_sessions()
        if _vid > 0:
            out["skipped"] = f"plex streaming {_vid} video session(s) — yielding"
            return out

    # ── 2c. Stale-acquisition recovery — rescue files stranded in old _acq_* dirs
    # (sits behind the gates above so it never competes with video streaming).
    try:
        _rec = recover_stale_acq_dirs()
        if (_rec.get("recovered") or _rec.get("dupes") or _rec.get("corrupt")
                or _rec.get("removed_dirs") or _rec.get("junk_dirs")):
            out["acq_recovery"] = _rec
            log.info("picker_tick: acq-recovery %s", _rec)
    except Exception:
        log.exception("picker_tick: acq recovery failed (continuing)")

    # ── 2b. 50/50 split: every other acquisition turn fills a partial album (the
    # "filler" tracks) instead of fetching a new liked-song album. Completion now runs
    # concurrently (atomic per-album claim), so across the picker's slots roughly half
    # the download attempts are liked songs and half are album filler. Falls through to
    # a normal liked acquire when there's nothing left to complete, so no slot is wasted.
    if (get_config("autofill_5050_filler", "1") or "1") == "1":
        try:
            with session_scope() as _qs:
                _qd = _qs.query(AutofillAction).filter(AutofillAction.status == "queued").count()
        except Exception:
            _qd = 1
        if _smart_split_should_fill(_qd):
            try:
                _fres = complete_albums_tick(batch=1)
            except Exception:
                log.exception("picker_tick: album-filler turn failed")
                _fres = {}
            if _fres.get("attempted") or _fres.get("completed_more"):
                out["picked"] = True
                out["filler"] = _fres
                return out
            # nothing to complete this turn — fall through to a normal liked acquire.

    # ── 3. Row selection — atomic claim: UPDATE sets status='downloading' so
    # concurrent workers cannot pick the same row.
    target = None
    _max_attempts = _max_picker_attempts()
    with session_scope() as s:
        _base = (select(AutofillAction)
                 .where(AutofillAction.status == "queued")
                 .where((AutofillAction.attempt_count == None) | (AutofillAction.attempt_count < _max_attempts)))
        # PRIORITIZE NEW ADDITIONS: never-attempted rows first, NEWEST-queued first — a song that
        # just showed up at the end of a playlist / Liked Songs is tried immediately instead of
        # waiting behind the backlog. Then the retry backlog, oldest-attempted first (unchanged).
        fresh = list(s.scalars(
            _base.where((AutofillAction.attempt_count == None) | (AutofillAction.attempt_count == 0))
                 .order_by(AutofillAction.created_at.desc())
                 .limit(50)
        ).all())
        backlog = list(s.scalars(
            _base.where(AutofillAction.attempt_count > 0)
                 .order_by(AutofillAction.last_attempt_at.asc())
                 .limit(50)
        ).all())
        candidates = fresh + backlog
        with _ACQUISITION_LOCK:
            in_flight_ids = set(CURRENT_ACQUISITIONS.keys())
        candidates = [c for c in candidates if c.id not in in_flight_ids]
        # RETRY BACKOFF: the hard-tail rows (attempt 15-45) were recycled every few
        # minutes forever, re-hammering every source (and the live-Spotify helpers)
        # without any chance of a different outcome. Exponential per-row cooldown:
        # attempts 0-2 retry immediately; then 5m, 10m, 20m, ... capped at 6h. New
        # songs always flow instantly; the tail retries a few times a day instead
        # of hundreds.
        _bo_now = datetime.utcnow()
        def _cooled_down(c):
            att = c.attempt_count or 0
            if att <= 2 or not c.last_attempt_at:
                return True
            cool_min = min(360, 5 * (2 ** min(att - 3, 7)))
            return (_bo_now - c.last_attempt_at) >= timedelta(minutes=cool_min)
        candidates = [c for c in candidates if _cooled_down(c)]
        # 50/50 bias: prefer a Spotify-liked row on every OTHER pick, so about half of
        # what we attempt is from the user's actual Liked Songs (not stray catalog rows).
        global _PICK_RATIO_COUNTER
        _liked_ids = _get_liked_track_ids()
        def _row_is_liked(c):
            try:
                _tids = json.loads(c.track_ids_json or "[]")
            except Exception:
                _tids = []
            return any(t in _liked_ids for t in _tids)
        _liked_cands = [c for c in candidates if _row_is_liked(c)]
        _other_cands = [c for c in candidates if c not in _liked_cands]
        # Acquire turns focus on the MAIN liked songs (the album-filler is now handled by
        # the completion turn above), so liked rows go first; other rows only when no
        # liked rows remain.
        ordered = _liked_cands + _other_cands
        for cand in ordered:
            # Atomic claim — only succeeds if no other worker has flipped status yet
            result = s.execute(
                update(AutofillAction)
                .where(AutofillAction.id == cand.id)
                .where(AutofillAction.status == "queued")
                .values(status="downloading", last_attempt_at=datetime.utcnow())
            )
            if result.rowcount == 1:
                target = cand
                row_id = cand.id
                artist = cand.artist
                album = cand.album
                foreign_album_id = cand.foreign_album_id
                track_ids_json = cand.track_ids_json
                _claim_prev_quality = cand.quality_acquired or ""
                _claim_was_imported = bool(cand.imported_paths)
                _PICK_RATIO_COUNTER += 1
                break
            # else: another worker raced us — try next candidate

    if not target:
        out["skipped"] = "no claimable queued row"
        return out

    out["checked"] += 1
    out["target"] = f"{artist} / {album}"
    mode = (get_config("autofill_acquisition_mode", "album") or "album").lower()
    log.info("picker_tick: trying %s / %s (mode=%s)", artist, album, mode)

    # ── 4. Resolve a Spotify URL for this row
    spotify_url: Optional[str] = None
    track_ids: list[str] = []
    if track_ids_json:
        try:
            track_ids = json.loads(track_ids_json) or []
        except Exception:
            track_ids = []
    _disp = _disputed_sources(track_ids)

    if mode == "song":
        # Song mode: just pass the first track URL — SpotiFLAC handles it
        if track_ids:
            spotify_url = f"https://open.spotify.com/track/{track_ids[0]}"
        else:
            out["skipped"] = "song mode but no track_ids in row"
            return out
    else:
        # album / discography mode: prefer a full album URL — resolved from LOCAL DATA
        # ONLY. No live Spotify during acquisition (the catalog mirror + cached fields
        # drive this; the gentle spotify_catalog_sync_tick is the sole live caller).
        if not track_ids:
            out["skipped"] = f"{mode} mode but row has no track_ids"
            return out
        # 1) a previously-resolved Spotify album id stored on the row ("sp:<id>")
        if foreign_album_id and str(foreign_album_id).startswith("sp:"):
            spotify_url = f"https://open.spotify.com/album/{str(foreign_album_id)[3:]}"
        # 2) the local catalog mirror — best real album containing this liked track
        if not spotify_url:
            try:
                from . import spotify_catalog
                best = spotify_catalog.best_album_for_liked(track_ids[0])
                if best and best.get("album_url"):
                    spotify_url = best["album_url"]
                    if best.get("track_ids"):
                        track_ids = best["track_ids"]
                    # File the song under the BIGGEST album (placement + tags follow), so it
                    # lands in that album's folder instead of a smaller single/release.
                    if best.get("name"):
                        album = best["name"]
                    _journey_log.append("mirror-album:%dtrk" % (best.get("total_tracks") or 0))
            except Exception:
                log.exception("picker_tick: mirror album lookup failed (continuing)")
        # 3) fallback: single-track URL (no Spotify) — same as today until the mirror fills
        if not spotify_url:
            spotify_url = f"https://open.spotify.com/track/{track_ids[0]}"

    out["spotify_url"] = spotify_url

    # ── 4a. HARD BLOCK generic hits compilations ("Now That's What I Call Music!" & friends).
    # best_album_for_liked already refuses to acquire a comp, so `album` here is either a real
    # album or (when the mirror only knew the comp) the comp name itself. Never build a comp
    # folder: park the row so it isn't retried, and don't acquire it.
    try:
        from . import spotify_catalog as _sc_blk
        if _sc_blk.is_blocked_comp_name(album):
            with session_scope() as _bs:
                _br = _bs.query(AutofillAction).filter(AutofillAction.id == row_id).first()
                if _br is not None:
                    _br.status = "abandoned"
                    _br.note = "blocked: generic hits compilation — not created"
                    _bs.commit()
            out["skipped"] = "blocked: generic hits compilation"
            out["picked"] = False
            log.info("picker_tick: BLOCKED compilation album %r — not creating it", album)
            return out
    except Exception:
        log.exception("picker_tick: compilation-block check failed (continuing)")

    # ── 4b. Dedup-aware quality gate. If we ALREADY own this song (the global ledger, or
    # this row's own prior import), don't burn a CD/Telegram re-download — the only thing
    # worth fetching is a genuine HI-RES upgrade. So:
    #   • already own hi-res  → skip the whole acquire, park as library_existing.
    #   • already own CD only → "_upgrade_only": attempt hi-res ONLY (no CD/Soulseek/Telegram
    #                            fallbacks), and keep the result only if it's actually hi-res.
    # This is exactly the "tried & failed an upgrade, then needlessly re-grabbed CD" bug.
    _upgrade_only = False
    try:
        if _dedup_enabled():
            with session_scope() as _own_s:
                _owned_rank = _ledger_rank_for_row(_own_s, track_ids, artist)
            _prev_rank = _quality_rank(_claim_prev_quality) if _claim_was_imported else 0
            _owned_rank = max((_owned_rank if _owned_rank is not None else -1), _prev_rank)
            if _owned_rank >= 3:
                with session_scope() as _ps:
                    _pr = _ps.query(AutofillAction).filter(AutofillAction.id == row_id).first()
                    if _pr is not None:
                        _pr.status = "library_existing"
                        _pr.hires_upgrade_attempted = True
                        _pr.note = "deduped: already own hi-res — skipped re-download"
                        _ps.commit()
                out["skipped"] = "deduped: already own hi-res"
                out["picked"] = False
                log.info("picker_tick: dedup skip (own hi-res) %s / %s", artist, album)
                return out
            if _owned_rank == 2:
                _upgrade_only = True
                _journey_log.append("dedup:own-CD → hi-res-upgrade-only")
                log.info("picker_tick: dedup upgrade-only (own CD) %s / %s", artist, album)
    except Exception:
        log.exception("picker_tick: dedup quality gate failed (continuing normally)")

    # ── 5. Acquire via SpotiFLAC
    from .spotiflac_adapter import acquire as spotiflac_acquire
    log.info("picker_tick: calling spotiflac_acquire url=%s", spotify_url)
    # Compute tracks_total from row's track_ids (album mode) or 1 (track mode)
    try:
        _tracks_total = len(track_ids) if track_ids else (1 if mode == "track" else 0)
    except Exception:
        _tracks_total = 0
    # Per-row dest dir so the progress watcher can count files unambiguously
    import os as _os_acq, threading as _th_acq, time as _time_acq
    _acq_dir = f"/Volumes/MediaVolume3/Downloads/music/spotiflac/_acq_{row_id}_{int(_time_acq.time())}"
    try:
        _os_acq.makedirs(_acq_dir, exist_ok=True)
    except Exception as _mk_err:
        # C-8: never fall back to the shared dir — concurrent workers would stomp.
        log.exception("picker_tick: failed to create acq dir %s: %s", _acq_dir, _mk_err)
        out["skipped"] = f"acq_dir create failed: {_mk_err}"
        return out
    acq_entry = {
        "artist": artist,
        "album": album,
        "spotify_url": spotify_url,
        "started_at": datetime.utcnow().isoformat() + "Z",
        "row_id": row_id,
        "mode": mode,
        "tracks_total": _tracks_total,
        "tracks_done": 0,
        "acq_dir": _acq_dir,
        "quality_target": None,
        "quality_acquired": None,
    }
    global CURRENT_ACQUISITION
    with _ACQUISITION_LOCK:
        CURRENT_ACQUISITIONS[row_id] = acq_entry
        CURRENT_ACQUISITION = acq_entry  # bw-compat pointer
    # Spawn progress watcher (auto-stops when stop_event.set())
    _progress_stop = _th_acq.Event()
    _start_progress_watcher(row_id, _acq_dir, _tracks_total, _progress_stop)
    # === SOULSEEK PRIMARY ===
    # Try Soulseek before SpotiFLAC: when the 4 scraper providers (Tidal/Qobuz/Amazon/
    # Deezer) are down together, this is the only path that works. Soulseek is real
    # peer-to-peer file transfer — different infrastructure, different failure modes.
    # Disable by setting autofill_soulseek_primary=0 in config.
    res = None
    # === SQUID.WTF PRIMARY (Qobuz hi-res) — tried FIRST per song ===
    # squid is a direct CDN hi-res grab (~10s/track) vs Soulseek's slow p2p (up to 300s).
    # Trying it first drains the backlog faster AND lands 24-bit hi-res. Soulseek / SpotiFLAC /
    # Telegram remain per-song fallbacks for whatever squid can't find. First success wins —
    # no song is downloaded twice. Reversible: set autofill_squid_first=0 to restore old order.
    if get_config("autofill_squid_first", "1") == "1" and not _upgrade_only and "squid" not in _disp:
        try:
            from . import squid_adapter
            if squid_adapter.is_enabled() and not squid_adapter._in_break():
                with _ACQUISITION_LOCK:
                    if row_id in CURRENT_ACQUISITIONS:
                        CURRENT_ACQUISITIONS[row_id]["source"] = "squid"
                        CURRENT_ACQUISITIONS[row_id]["quality_target"] = "HI_RES_LOSSLESS"
                _journey_log.append("squid-primary:ATTEMPT")
                _sq = squid_adapter.acquire(track_ids=track_ids, artist=artist, album=album,
                                            dest_dir=_acq_dir, flac_only=True,
                                            timeout_seconds=min(160, max(40, len(track_ids or [1]) * 25)))
                if _sq.get("success") and _sq.get("paths"):
                    # squid fetches each track from whatever Qobuz album it sits on, so the
                    # file's ALBUM tag won't match the row's assigned album. The placement
                    # tag-gate (_tags_match_row) requires BOTH artist AND album to match, so
                    # it would reject every squid file and re-queue the row → infinite
                    # re-download loop. squid already verified the right ARTIST+TITLE, so
                    # re-stamp ONLY the album to the row's album here: the gate still checks
                    # artist (catching wrong deliveries), and the track files under the
                    # correct album. (Album-only — leaving artist intact preserves the gate.)
                    try:
                        from mutagen.flac import FLAC as _FLAC_sq
                        for _pp in _sq["paths"]:
                            try:
                                if album:
                                    _ff = _FLAC_sq(_pp); _ff["album"] = album; _ff.save()
                            except Exception:
                                pass
                    except Exception:
                        pass
                    from .spotiflac_adapter import AcquireResult as _AR_sq
                    import os as _os_sq
                    _tot = sum(_os_sq.path.getsize(pp) for pp in _sq["paths"] if _os_sq.path.exists(pp))
                    res = _AR_sq(success=True, paths=_sq["paths"], bytes_total=_tot,
                                 provider="squid", quality_requested="HI_RES_LOSSLESS", error=None,
                                 duration_seconds=0.0, raw_stdout_tail="[squid.wtf qobuz]")
                    _record_provider_success("squid")
                    _journey_log[-1] = "squid-primary:OK files=%d" % len(_sq["paths"])
                    log.info("picker_tick: squid.wtf PRIMARY delivered %d files for %s/%s",
                             len(_sq["paths"]), artist, album)
                    with _ACQUISITION_LOCK:
                        if row_id in CURRENT_ACQUISITIONS:
                            CURRENT_ACQUISITIONS[row_id]["quality_acquired"] = "HI_RES_LOSSLESS"
                else:
                    _journey_log[-1] = "squid-primary:FAIL (%s)" % ((_sq.get("error") or "no files")[:50])
        except Exception:
            log.exception("picker_tick: squid PRIMARY raised (continuing)")

    # === SOULSEEK (fallback — only when squid didn't already deliver) ===
    if get_config("autofill_soulseek_primary", "1") == "1" and not _upgrade_only \
            and not (res and res.success and res.paths) and "soulseek" not in _disp:
        _journey_log.append("soulseek-primary:ATTEMPT")
        log.info("picker_tick: trying Soulseek PRIMARY for %s / %s", artist, album)
        try:
            # Sample-song lookup for better search-query quality
            sample_song = None
            try:
                if track_ids:
                    from . import spotify_catalog
                    sample_song = spotify_catalog.liked_title(track_ids[0])  # cached, no live Spotify
            except Exception:
                pass
            with _ACQUISITION_LOCK:
                if row_id in CURRENT_ACQUISITIONS:
                    CURRENT_ACQUISITIONS[row_id]["quality_target"] = "HI_RES_LOSSLESS"
                    CURRENT_ACQUISITIONS[row_id]["provider"] = "soulseek-primary"
                    CURRENT_ACQUISITIONS[row_id]["source"] = "soulseek"
            from . import slskd_picker
            sl_res = slskd_picker.acquire_album(
                artist=artist, album=album,
                sample_song=sample_song,
                single_track_ok=(mode == "song"),
                expected_track_count=(len(track_ids) if track_ids else None),
                flac_only=True,
                download_dir=_acq_dir,
                timeout_seconds=300,  # was 180; slskd needs ~90s for search + transfer time
            )
            if sl_res.get("success") and sl_res.get("paths"):
                _journey_log[-1] = "soulseek-primary:OK files=%d peer=%s" % (
                    len(sl_res["paths"]), sl_res.get("peer") or "?")
                _record_provider_success("soulseek")
                log.info("picker_tick: Soulseek PRIMARY delivered %d files for %s/%s",
                         len(sl_res["paths"]), artist, album)
                from .spotiflac_adapter import AcquireResult as _AR_sp
                import os as _os_sp
                _total = sum(_os_sp.path.getsize(pp) for pp in sl_res["paths"] if _os_sp.path.exists(pp))
                res = _AR_sp(
                    success=True,
                    paths=sl_res["paths"],
                    bytes_total=_total,
                    provider=f"soulseek:{sl_res.get('peer', '?')}",
                    quality_requested="HI_RES_LOSSLESS",
                    error=None,
                    duration_seconds=sl_res.get("duration_seconds", 0.0),
                    raw_stdout_tail="[soulseek primary]",
                )
                with _ACQUISITION_LOCK:
                    if row_id in CURRENT_ACQUISITIONS:
                        CURRENT_ACQUISITIONS[row_id]["quality_acquired"] = "HI_RES_LOSSLESS"
            else:
                _journey_log[-1] = "soulseek-primary:FAIL (%s)" % (
                    (sl_res.get("error") or "no files")[:60])
                log.info("picker_tick: Soulseek primary failed for %s/%s; falling through to SpotiFLAC",
                         artist, album)
        except Exception as _sl_exc:
            log.exception("picker_tick: Soulseek primary raised: %s", _sl_exc)
            _journey_log[-1] = "soulseek-primary:EXCEPTION (%s)" % (str(_sl_exc)[:60])

    # === SQUID.WTF (Qobuz hi-res) — legacy 2nd-source slot ===
    # Only runs when squid is NOT configured as the primary (autofill_squid_first=0); otherwise
    # squid was already tried first above and re-trying here would be a wasted duplicate attempt.
    if not (res and res.success and res.paths) and not _upgrade_only \
            and get_config("autofill_squid_first", "1") != "1" and "squid" not in _disp:
        try:
            from . import squid_adapter
            if squid_adapter.is_enabled() and not squid_adapter._in_break():
                with _ACQUISITION_LOCK:
                    if row_id in CURRENT_ACQUISITIONS:
                        CURRENT_ACQUISITIONS[row_id]["source"] = "squid"
                _journey_log.append("squid:ATTEMPT")
                _sq = squid_adapter.acquire(track_ids=track_ids, artist=artist, album=album,
                                            dest_dir=_acq_dir, flac_only=True,
                                            timeout_seconds=min(160, max(40, len(track_ids or [1]) * 25)))
                if _sq.get("success") and _sq.get("paths"):
                    # squid fetches each track from whatever Qobuz album it sits on, so the
                    # file's ALBUM tag won't match the row's assigned album. The placement
                    # tag-gate (_tags_match_row) requires BOTH artist AND album to match, so
                    # it would reject every squid file and re-queue the row → infinite
                    # re-download loop. squid already verified the right ARTIST+TITLE, so
                    # re-stamp ONLY the album to the row's album here: the gate still checks
                    # artist (catching wrong deliveries), and the track files under the
                    # correct album. (Album-only — leaving artist intact preserves the gate.)
                    try:
                        from mutagen.flac import FLAC as _FLAC_sq
                        for _pp in _sq["paths"]:
                            try:
                                if album:
                                    _ff = _FLAC_sq(_pp); _ff["album"] = album; _ff.save()
                            except Exception:
                                pass
                    except Exception:
                        pass
                    from .spotiflac_adapter import AcquireResult as _AR_sq
                    import os as _os_sq
                    _tot = sum(_os_sq.path.getsize(pp) for pp in _sq["paths"] if _os_sq.path.exists(pp))
                    res = _AR_sq(success=True, paths=_sq["paths"], bytes_total=_tot,
                                 provider="squid", quality_requested="HI_RES_LOSSLESS", error=None,
                                 duration_seconds=0.0, raw_stdout_tail="[squid.wtf qobuz]")
                    _record_provider_success("squid")
                    _journey_log[-1] = "squid:OK files=%d" % len(_sq["paths"])
                    log.info("picker_tick: squid.wtf delivered %d files for %s/%s", len(_sq["paths"]), artist, album)
                    with _ACQUISITION_LOCK:
                        if row_id in CURRENT_ACQUISITIONS:
                            CURRENT_ACQUISITIONS[row_id]["quality_acquired"] = "HI_RES_LOSSLESS"
                else:
                    _journey_log[-1] = "squid:FAIL (%s)" % ((_sq.get("error") or "no files")[:50])
        except Exception:
            log.exception("picker_tick: squid source raised (continuing)")

    try:
        _quality = "LOSSLESS" if (get_config("autofill_allow_cd_quality", "0") == "1") else "HI_RES_LOSSLESS"
        if _upgrade_only:
            _quality = "HI_RES_LOSSLESS"   # already own CD — only a hi-res result is worth keeping
        with _ACQUISITION_LOCK:
            if row_id in CURRENT_ACQUISITIONS:
                CURRENT_ACQUISITIONS[row_id]["quality_target"] = _quality
                # If Soulseek-primary didn't already deliver, we're about to hit
                # SpotiFLAC — reflect that in the live "Right now" source chip.
                if not (res and res.success and res.paths):
                    CURRENT_ACQUISITIONS[row_id]["source"] = "spotiflac"
        if not (res and res.success and res.paths) and "spotiflac" not in _disp:
            res = spotiflac_acquire(
                spotify_url,
                dest_dir=_acq_dir,
                quality=_quality,
                timeout_seconds=90,  # was 180; faster failure when providers degraded
            )
        # AUTO-CD FALLBACK: when hi-res fails, automatically retry with LOSSLESS.
        # Many albums simply aren't available in 24/96 anywhere; CD-quality FLAC
        # is still high quality and usually available. Only attempted when the
        # user hasn't explicitly forced LOSSLESS already.
        if (not res.success or not res.paths) and _quality == "HI_RES_LOSSLESS" and not _upgrade_only:
            _journey_log.append("hires:FAIL retry-as-LOSSLESS")
            log.info("picker_tick: hi-res failed; retrying as LOSSLESS for %s / %s", artist, album)
            res = spotiflac_acquire(
                spotify_url,
                dest_dir=_acq_dir,
                quality="LOSSLESS",
                timeout_seconds=90,  # was 180
            )
            with _ACQUISITION_LOCK:
                if row_id in CURRENT_ACQUISITIONS:
                    CURRENT_ACQUISITIONS[row_id]["quality_target"] = "LOSSLESS"
            if res.success and res.paths:
                _journey_log.append("LOSSLESS:OK files=%d" % len(res.paths))
                with _ACQUISITION_LOCK:
                    if row_id in CURRENT_ACQUISITIONS:
                        CURRENT_ACQUISITIONS[row_id]["quality_acquired"] = "LOSSLESS"
            else:
                _journey_log.append("LOSSLESS:FAIL (%s)" % ((res.error or "no files")[:60]))
        if res.success and res.paths:
            _journey_log.append("spotiflac:OK files=%d provider=%s" % (len(res.paths), res.provider or "?"))
            _record_provider_success("spotiflac")
            # Record actual delivered quality on acq_entry for dashboard + DB later
            try:
                quality_acquired_actual = "HI_RES_LOSSLESS" if _quality == "HI_RES_LOSSLESS" else "LOSSLESS"
                with _ACQUISITION_LOCK:
                    if row_id in CURRENT_ACQUISITIONS:
                        CURRENT_ACQUISITIONS[row_id]["quality_acquired"] = quality_acquired_actual
            except Exception:
                pass
        else:
            _journey_log.append("spotiflac:FAIL (%s)" % ((res.error or "no files")[:60]))

        # ── THIRD SOURCE: Telegram (@BeatSpotBot) ───────────────────────────────
        # Tried when Soulseek + SpotiFLAC both came up empty. Independent
        # infrastructure (a Telegram bot driven via a user session), so it often
        # delivers FLAC when the mirrors are down. Only runs when configured.
        if (not res or not res.success or not res.paths) and not _upgrade_only:
            try:
                from . import telegram_picker
                if telegram_picker.is_configured():
                    _journey_log.append("telegram:ATTEMPT")
                    log.info("picker_tick: trying Telegram (@BeatSpotBot) for %s / %s", artist, album)
                    with _ACQUISITION_LOCK:
                        if row_id in CURRENT_ACQUISITIONS:
                            CURRENT_ACQUISITIONS[row_id]["provider"] = "telegram"
                            CURRENT_ACQUISITIONS[row_id]["source"] = "telegram"
                    _tg_sample = None
                    try:
                        if track_ids:
                            from . import spotify_catalog as _sc_tg
                            _tg_sample = _sc_tg.liked_title(track_ids[0])
                    except Exception:
                        pass
                    # Serialize against album-completion's Telegram use (one bot chat).
                    if not _TELEGRAM_SEM.acquire(blocking=False):
                        tg = {"success": False, "paths": [],
                              "error": "telegram busy (another search in progress)"}
                    else:
                        try:
                            tg = telegram_picker.acquire(
                                spotify_url=spotify_url, artist=artist, album=album,
                                sample_song=_tg_sample, dest_dir=_acq_dir,
                                flac_only=True, timeout_seconds=240)
                        finally:
                            try: _TELEGRAM_SEM.release()
                            except Exception: pass
                    if tg.get("success") and tg.get("paths"):
                        from .spotiflac_adapter import AcquireResult as _AR_tg
                        import os as _os_tg
                        # The bot delivers ISRC-named files with ISRC titles via this path.
                        # Stamp the requested song title and rename the file so it isn't named
                        # a number on disk / in the feed / in Plex.
                        if _tg_sample:
                            _safe_tg = re.sub(r'[/\\:*?"<>|]', '-', _tg_sample).strip().rstrip('.')[:120]
                            _fixed = []
                            for _fp in tg["paths"]:
                                try:
                                    from mutagen.flac import FLAC as _FLAC_tg
                                    _ff = _FLAC_tg(_fp); _ff["title"] = _tg_sample; _ff.save()
                                except Exception:
                                    pass
                                _np = _fp
                                if _safe_tg:
                                    _cand = _os_tg.path.join(_os_tg.path.dirname(_fp), _safe_tg + _os_tg.path.splitext(_fp)[1])
                                    try:
                                        if _cand != _fp and not _os_tg.path.exists(_cand):
                                            _os_tg.rename(_fp, _cand); _np = _cand
                                    except Exception:
                                        pass
                                _fixed.append(_np)
                            tg["paths"] = _fixed
                        _tot = sum(_os_tg.path.getsize(pp) for pp in tg["paths"] if _os_tg.path.exists(pp))
                        res = _AR_tg(success=True, paths=tg["paths"], bytes_total=_tot,
                                     provider="telegram:@BeatSpotBot", quality_requested="LOSSLESS",
                                     error=None, duration_seconds=0.0, raw_stdout_tail="[telegram]")
                        _record_provider_success("telegram")
                        _journey_log[-1] = "telegram:OK files=%d" % len(tg["paths"])
                        with _ACQUISITION_LOCK:
                            if row_id in CURRENT_ACQUISITIONS:
                                CURRENT_ACQUISITIONS[row_id]["quality_acquired"] = "LOSSLESS"
                    else:
                        _journey_log[-1] = "telegram:FAIL (%s)" % ((tg.get("error") or "no files")[:60])
            except Exception:
                log.exception("picker_tick: telegram source failed (continuing)")

        # FAIL FAST: if the primary + AUTO-CD attempts indicate a GLOBAL
        # provider outage (not a per-album miss), skip the entire fallback chain.
        # We're not going to recover by trying URL variants when every provider
        # is returning UNAVAILABLE — we just waste 5–10 min per album.
        if (not res.success or not res.paths) and _is_provider_outage(getattr(res, "error", None)):
            _journey_log.append("FAIL-FAST: provider outage detected; skipping fallback chain")
            log.info("picker_tick: provider outage on %s/%s — short-circuiting (error=%r)",
                     artist, album, (res.error or "")[:120])
            try:
                with session_scope() as _os:
                    _r = _os.get(AutofillAction, row_id)
                    if _r is not None:
                        _r.status = "queued"
                        _r.attempt_count = (_r.attempt_count or 0) + 1
                        _r.last_attempt_at = datetime.utcnow()
                        _r.note = ("provider outage: " + (res.error or "")[:200])[:500]
                        _os.commit()
            except Exception:
                log.exception("picker_tick: provider-outage requeue failed")
            out["picked"] = False
            out["error"] = "provider_outage"
            out["elapsed_s"] = round((getattr(res, "duration_seconds", 0.0) or 0.0), 1)
            # Record outcome for circuit breaker (uses the new fast path)
            try:
                _record_picker_outcome("provider_failure")
            except Exception:
                pass
            # Dashboard: animate this album fading out of "Right now" (red).
            try:
                _record_outcome(row_id, "fail", artist, album, "spotiflac", "provider outage")
            except Exception:
                pass
            return out

        # (Album-name-variant retry removed: it needed a live Spotify search during
        # acquisition, which is disabled — the block was a no-op that still logged
        # misleading "trying URL variant" lines and an empty journey entry.)

        # B. Track-URL fallback: if album-mode call failed, try downloading the
        # originating track(s) individually. Many albums fail as a whole because
        # one constituent track is unavailable on the chosen provider; a per-track
        # download often succeeds for the track the user actually liked.
        if (not res.success or not res.paths) and mode in ("album", "discography") and track_ids:
            log.info("picker_tick: album-mode failed (%s), trying track-URL fallback (%d tracks)",
                     res.error or "no files", min(len(track_ids), 5))
            from .spotiflac_adapter import AcquireResult as _AR
            fallback_paths = []
            fallback_provider = None
            for _tid in track_ids[:5]:
                try:
                    _tr = spotiflac_acquire(
                        f"https://open.spotify.com/track/{_tid}",
                        dest_dir="/Volumes/MediaVolume3/Downloads/music/spotiflac",
                        quality="HI_RES_LOSSLESS",
                        timeout_seconds=60,  # was 180; per-track shouldn't need much
                    )
                    if _tr.success and _tr.paths:
                        fallback_paths.extend(_tr.paths)
                        fallback_provider = fallback_provider or _tr.provider
                except Exception:
                    log.exception("picker_tick: track-URL fallback for %s raised", _tid)
            if fallback_paths:
                _journey_log.append("track-fallback:OK files=%d" % len(fallback_paths))
                log.info("picker_tick: track-URL fallback recovered %d files", len(fallback_paths))
                import os as _os
                _total = sum(_os.path.getsize(p) for p in fallback_paths if _os.path.exists(p))
                res = _AR(
                    success=True,
                    paths=fallback_paths,
                    bytes_total=_total,
                    provider=fallback_provider or res.provider,
                    quality_requested=res.quality_requested,
                    error=None,
                    duration_seconds=res.duration_seconds,
                    raw_stdout_tail=(res.raw_stdout_tail or "") + "\n[track-URL fallback]",
                )
            else:
                _journey_log.append("track-fallback:FAIL")
        # C. Soulseek fallback: last resort for albums neither SpotiFLAC album-mode
        # nor SpotiFLAC track-mode could deliver. Disabled by default; enable via
        # autofill_soulseek_fallback_enabled = 1.
        # FAIL FAST: skip Soulseek too if errors indicate provider-wide outage.
        if (not res.success or not res.paths) and not _upgrade_only and \
           (get_config("autofill_soulseek_fallback_enabled", "1") == "1") and \
           not _is_provider_outage(getattr(res, "error", None)):
            log.info("picker_tick: SpotiFLAC exhausted, trying Soulseek fallback for %s / %s",
                     artist, album)
            _journey_log.append("soulseek:ATTEMPT")
            try:
                from . import slskd_picker
                sl_res = slskd_picker.acquire_album(
                    artist=artist, album=album,
                    single_track_ok=(mode == "song"),
                    flac_only=True,
                    download_dir="/Volumes/MediaVolume3/Downloads/music/spotiflac",
                    timeout_seconds=120,  # was 300
                )
                if sl_res.get("success") and sl_res.get("paths"):
                    _journey_log[-1] = "soulseek:OK files=%d peer=%s" % (len(sl_res["paths"]), sl_res.get("peer") or "?")
                    log.info("picker_tick: Soulseek delivered %d files via peer=%s",
                             len(sl_res["paths"]), sl_res.get("peer"))
                    from .spotiflac_adapter import AcquireResult as _AR
                    import os as _os
                    _total = sum(_os.path.getsize(p) for p in sl_res["paths"] if _os.path.exists(p))
                    res = _AR(
                        success=True,
                        paths=sl_res["paths"],
                        bytes_total=_total,
                        provider=f"soulseek:{sl_res.get('peer', '?')}",
                        quality_requested="HI_RES_LOSSLESS",
                        error=None,
                        duration_seconds=sl_res.get("duration_seconds", 0.0),
                        raw_stdout_tail="[soulseek fallback]",
                    )
            except Exception:
                log.exception("picker_tick: Soulseek fallback raised")
    finally:
        # C-1/C-3: stop watcher first (Event.set is thread-safe, no need to hold lock).
        try: _progress_stop.set()
        except Exception: pass
        with _ACQUISITION_LOCK:
            CURRENT_ACQUISITIONS.pop(row_id, None)
            if CURRENT_ACQUISITIONS:
                CURRENT_ACQUISITION = max(
                    CURRENT_ACQUISITIONS.values(),
                    key=lambda d: d.get("started_at", ""),
                )
            else:
                CURRENT_ACQUISITION = None


    # ── 6. Post-acquire orphan sweep — move anything that landed in /Volumes/MediaVolume3/Downloads/music
    try:
        post_sweep = sweep_orphan_downloads()
        if post_sweep.get("swept", 0) > 0:
            log.info("picker_tick: post-sweep moved %d files", post_sweep["swept"])
            out["post_sweep"] = post_sweep
    except Exception:
        log.exception("picker_tick: post-sweep failed (continuing)")

    # ── 7. Reflect result in output dict
    out["picked"] = res.success
    out["acquired_files"] = len(res.paths)
    out["elapsed_s"] = round(res.duration_seconds, 1)
    # Feed the smart ticker: how long this download lane was actually busy.
    try:
        if out.get("target") and res and res.duration_seconds and res.duration_seconds > 0:
            _record_acquire_seconds(res.duration_seconds)
    except Exception:
        pass
    # Record outcome for circuit-breaker + AIMD-concurrency decisions
    # BUGFIX: this used out["imported_files"], a key that's never set — so a real success
    # was never recorded, leaving the circuit breaker unable to reset and the concurrency
    # auto-tuner unable to ramp up. Use moved_files (files actually placed in /Volumes/MediaVolume3/plexify-music).
    try:
        if out.get("picked") and out.get("moved_files", 0) > 0:
            _record_picker_outcome("success")
        elif out.get("target") and not out.get("picked"):
            # We picked a target but acquire failed — provider failure category
            _record_picker_outcome("provider_failure")
        elif "skipped" in out:
            _record_picker_outcome("skipped")
        else:
            _record_picker_outcome("no_candidates")
    except Exception:
        pass
    out["provider"] = res.provider
    out["quality"] = res.quality_requested
    if res.error:
        out["error"] = res.error

    # Source attribution for dashboard chips (soulseek vs spotiflac + mirror/version)
    try:
        _src, _src_detail = derive_source(res.provider)
    except Exception:
        _src, _src_detail = (None, None)
    out["source"] = _src
    out["source_detail"] = _src_detail

    # ── 8. Persist row update
    with session_scope() as s:
        row = s.get(AutofillAction, row_id)
        if not row:
            return out
        # Capture pre-update state to detect a hi-res UPGRADE (low-end FLAC → hi-res)
        # so the dashboard can glow GOLD on this departure instead of green.
        _prev_quality = (row.quality_acquired or "")
        _was_imported = bool(row.imported_paths)
        row.last_attempt_at = datetime.utcnow()
        # ── DB-LOCK FIX (#4): release this transaction NOW, before the slow file work.
        # Everything below — ffmpeg integrity verify + shutil.move of every FLAC +
        # tag-stamp + cover download — is filesystem/network I/O that took seconds while
        # this session kept its (read) transaction open. In WAL mode that pinned the WAL
        # snapshot, blocking checkpoints -> the WAL bloated -> writes slowed -> the OTHER
        # picker slots hit busy_timeout and threw "database is locked". Committing here
        # ends the transaction (and persists last_attempt_at). expire_on_commit=False keeps
        # `row` fully usable; the row.status/note/imported_paths writes below mark it dirty
        # and flush in ONE short commit at block exit (no `s` query runs in between, so no
        # autoflush reopens a transaction during the I/O).
        s.commit()
        # UPGRADE-ONLY dedup gate: we already own this song at CD, so the ONLY worthwhile
        # result is a genuine hi-res copy. If the (hi-res-only) attempts didn't deliver one,
        # this is a same-or-lower duplicate — discard the temp download and park the row as
        # library_existing (success-equivalent) so it's never re-attempted. This is the
        # "tried & failed an upgrade, then needlessly re-grabbed CD/Telegram" fix.
        if _upgrade_only:
            _got_paths = list(res.paths) if (res and res.success and res.paths) else []
            _got_hires = bool(_got_paths) and _quality_rank(_actual_flac_quality(_got_paths)) >= 3
            if not _got_hires:
                for _p in _got_paths:
                    try:
                        if os.path.isfile(_p):
                            os.remove(_p)
                    except Exception:
                        pass
                row.status = "library_existing"
                row.hires_upgrade_attempted = True
                row.note = "deduped: own CD, no hi-res upgrade available"
                out["skipped"] = "deduped: own CD, no hi-res upgrade"
                out["picked"] = False
                log.info("picker_tick: upgrade-only no-op for %s/%s — own CD, no hi-res; parked", artist, album)
                return out
            # genuine hi-res in hand → fall through to the normal import (a real upgrade).
        # DURATION GATE (homonym protection, 2026-06-15): for each delivered file, if it
        # claims a liked song's title but its length is wildly off that song's Spotify
        # duration, it is a same-name HOMONYM (a different recording), not what the user
        # liked — drop it. Per-file + title-matched so album filler is unaffected; threshold
        # is deliberately loose (>25s AND >18% off) so legit radio-edit/remaster length
        # differences sail through. No-op until liked durations are populated by the sync.
        if res and res.success and res.paths and track_ids:
            try:
                from .db import SpotifyLikedTrack as _SLT
                from mutagen import File as _MF2
                with SessionLocal() as _ds2:
                    _liked_dur = {_norm_title_key(l.title or ""): int(l.duration_ms or 0)
                                  for l in _ds2.query(_SLT).filter(_SLT.spotify_track_id.in_(track_ids)).all()
                                  if (l.duration_ms or 0) > 0}
                if _liked_dur:
                    _keep = []
                    for _fp in res.paths:
                        try:
                            _mf = _MF2(_fp, easy=True)
                            _ft = (_mf.tags.get("title") or [""])[0] if (_mf and _mf.tags) else ""
                            _fl = int((getattr(_MF2(_fp).info, "length", 0) or 0) * 1000)
                        except Exception:
                            _ft, _fl = "", 0
                        _exp = _liked_dur.get(_norm_title_key(_ft), 0)
                        if _exp > 0 and _fl > 0 and abs(_fl - _exp) > 25000 and abs(_fl - _exp) > 0.18 * _exp:
                            log.warning("picker_tick: DURATION MISMATCH %r %s/%s — expected %.0fs got %.0fs (likely homonym); dropping",
                                        _ft, artist, album, _exp / 1000.0, _fl / 1000.0)
                            _journey_log.append("duration-gate:drop %r exp=%ds got=%ds" % (_ft[:24], _exp // 1000, _fl // 1000))
                            try:
                                if os.path.isfile(_fp):
                                    os.remove(_fp)
                            except Exception:
                                pass
                        else:
                            _keep.append(_fp)
                    if not _keep:
                        res.success = False
                    res.paths = _keep
            except Exception:
                log.exception("picker_tick: duration gate error (continuing)")
        if res.success and res.paths:
            # Tag-verified placement: confirm SpotiFLAC/Soulseek delivered what
            # was actually requested. If file tags say "Danny Elfman" but the
            # row asked for "Surfaces", REJECT — don't mis-attribute in /Volumes/MediaVolume3/plexify-music.
            # INTEGRITY GATE: ffmpeg-decode each FLAC before placement.
            if res.success and res.paths:
                _intact, _bad_flacs = _verify_flac_integrity(res.paths)
                if _bad_flacs:
                    log.warning("picker_tick: %d/%d files failed integrity for %s/%s; removing",
                                len(_bad_flacs), len(res.paths), artist, album)
                    _journey_log.append("integrity:%d-bad-of-%d" % (len(_bad_flacs), len(res.paths)))
                    for _bp, _ber in _bad_flacs:
                        log.info("  bad file: %s -- %s", _bp, _ber[:120])
                        try:
                            if os.path.isfile(_bp):
                                os.remove(_bp)
                        except Exception:
                            log.exception("integrity-gate: failed to remove %s", _bp)
                    try:
                        res.paths = _intact
                        res.bytes_total = sum(os.path.getsize(p) for p in _intact if os.path.exists(p))
                        if not _intact:
                            res.success = False
                            res.error = (res.error or "") + " | integrity-gate: 0 intact files"
                    except Exception:
                        log.exception("integrity-gate: couldn't mutate res")
            _tag_passing, _tag_failing = _tag_based_placement(res.paths, artist, album)
            if _tag_failing:
                log.warning("picker_tick: %d/%d files have wrong tags for %s/%s — deleting",
                            len(_tag_failing), len(res.paths), artist, album)
                for _p, _reason in _tag_failing:
                    try:
                        import os as _osd
                        _osd.remove(_p)
                    except OSError:
                        pass
            if not _tag_passing:
                log.warning("picker_tick: ALL files mismatched tags; marking row queued for retry")
                row.status = "queued"
                row.note = "tag mismatch: provider delivered wrong tracks"
                row.attempt_count = (row.attempt_count or 0) + 1
                out["acquired_files"] = 0
                out["error"] = "tag mismatch"
                try:
                    _record_outcome(row_id, "fail", artist, album, _src, "wrong tracks (tag mismatch)")
                except Exception:
                    pass
                return out
            # Continue with only the passing files
            res.paths = _tag_passing
            res.bytes_total = sum(__import__("os").path.getsize(p) for p in _tag_passing if __import__("os").path.exists(p))

            # ── Move files to /Volumes/MediaVolume3/plexify-music/{Artist}/{Album}/ BEFORE the row is
            # updated, so the post-acquire sweep finds nothing to attribute
            # and cannot create phantom "Unknown Artist" duplicate rows.
            import shutil as _shutil

            def _safe_for_fs(name, default="Unknown"):
                """Sanitize a string for use as a filesystem path component."""
                name = (name or "").strip().rstrip(". ")
                for ch in ["/", "\\", "<", ">", ":", '"', "|", "?", "*"]:
                    name = name.replace(ch, "-")
                return (name[:200] or default).strip(". ")

            # ROOT-CAUSE FIX (album fragmentation): place + tag under the CANONICAL
            # album artist, not the per-track performer. This stops multi-performer
            # soundtracks (Dr. Horrible) and featured-artist singles from splitting
            # into one tile per credit. Falls back to the track artist when unsure.
            _album_artist, _aa_confident = _canonical_album_artist(
                artist, album, track_ids, getattr(row, "foreign_album_id", None))
            safe_artist = _safe_for_fs(_album_artist, "Unknown Artist")
            safe_album  = _safe_for_fs(album,  "Unknown Album")
            dest_dir    = f"/Volumes/MediaVolume3/plexify-music/{safe_artist}/{safe_album}"
            moved_paths: list[str] = []
            try:
                os.makedirs(dest_dir, exist_ok=True)
                os.chmod(dest_dir, 0o775)
                try:
                    os.chmod(os.path.dirname(dest_dir), 0o775)
                except Exception:
                    pass
            except Exception as _mkdir_err:
                log.warning("picker_tick: could not create dest dir %s: %s", dest_dir, _mkdir_err)

            _skipped_dupe = 0
            for src_path in res.paths:
                dest_path = os.path.join(dest_dir, os.path.basename(src_path))
                if os.path.exists(dest_path):
                    # Already there (e.g. re-run) — count it without re-moving
                    moved_paths.append(dest_path)
                    continue
                try:
                    if not os.path.exists(src_path):
                        log.warning("picker_tick: src missing, skipping: %s", src_path)
                        continue
                    # GLOBAL song dedup: when we pull a (now bigger) album for one liked song,
                    # its co-tracks are often songs we already own elsewhere. Skip any such
                    # co-track unless it's a strict upgrade — deletes only the temp download.
                    if _dedup_enabled():
                        with SessionLocal() as _ds:
                            _is_dupe = _ledger_is_dupe(_ds, src_path)[0]
                        if _is_dupe:
                            _skipped_dupe += 1
                            try: os.remove(src_path)
                            except Exception: pass
                            continue
                    _shutil.move(src_path, dest_path)
                    try:
                        os.chmod(dest_path, 0o664)
                    except Exception:
                        pass
                    moved_paths.append(dest_path)
                    log.info("picker_tick: moved %s -> %s", src_path, dest_path)
                except Exception as _mv_err:
                    log.warning("picker_tick: move failed %s -> %s: %s", src_path, dest_path, _mv_err)

            # TAG-STAMP (Soulseek-untagged fix): peers and degraded provider
            # fallbacks deliver FLACs with empty embedded tags → Plex "[Unknown
            # Album]". Stamp the album/artist we explicitly requested onto any
            # file missing them. Fill-only (never clobbers good tags), best-effort.
            for _mp in moved_paths:
                try:
                    _stamp_file_tags(_mp, artist, album,
                                     album_artist=(_album_artist if _aa_confident else None))
                except Exception:
                    log.exception("picker_tick: tag-stamp failed for %s", _mp)
                # Provenance: stamp the actual delivering source on each file.
                try:
                    _set_source_tag(_mp, _src)
                except Exception:
                    pass

            out["moved_files"] = len(moved_paths)
            # Album cover download (best-effort)
            try:
                if moved_paths:
                    download_album_cover(row, dest_dir)
            except Exception:
                log.exception("picker_tick: download_album_cover raised")
            log.info("picker_tick: moved %d/%d files to %s", len(moved_paths), len(res.paths), dest_dir)

            # D-3 + post-incident-sweep: only mark imported if we got the FULL set of
            # tracks. Partials produce broken albums in Plex; better to retry whole.
            _expected_tracks = len(track_ids) if track_ids else (1 if mode == "track" else 1)
            continue_to_finally = False   # True for requeue/fail outcomes -> skip import finalization
            if not moved_paths and _skipped_dupe > 0:
                # We pulled a (bigger) album but EVERY track it still needed is a song we
                # already own elsewhere — nothing new to keep. Park as library_existing
                # (success-equivalent) instead of requeuing into an endless re-download.
                row.status = "library_existing"
                row.note = (row.note or "")[:400] + (" | deduped: all %d tracks already owned" % _skipped_dupe)
                row.imported_paths = None
                row.total_size_bytes = None
            elif not moved_paths:
                row.status = "queued"
                row.note = (row.note or "")[:400] + " | all moves failed; requeued"
                continue_to_finally = True
                try:
                    _record_outcome(row_id, "fail", artist, album, _src, "file move failed")
                except Exception:
                    pass
            elif (not _dedup_enabled()) and len(moved_paths) < _expected_tracks:
                # Legacy (dedup OFF): require the FULL album — clean partials + requeue.
                for _p in moved_paths:
                    try:
                        if os.path.isfile(_p):
                            os.remove(_p)
                    except Exception:
                        log.exception("partial-cleanup: failed to remove %s", _p)
                row.status = "queued"
                row.note = (row.note or "")[:400] + (" | partial %d/%d; cleaned + requeued" % (len(moved_paths), _expected_tracks))
                log.info("picker_tick: PARTIAL %d/%d for %s/%s — cleaned + requeued",
                         len(moved_paths), _expected_tracks, artist, album)
                row.imported_paths = None
                row.total_size_bytes = None
                try:
                    _record_outcome(row_id, "fail", artist, album, _src,
                                    "partial %d/%d tracks" % (len(moved_paths), _expected_tracks))
                except Exception:
                    pass
                continue_to_finally = True
            else:
                # dedup ON: keep whatever NEW tracks we got (owned co-tracks were skipped);
                # completion fills any genuinely-missing remainder later. No more delete +
                # requeue churn for one liked song that lives on a giant compilation.
                row.status = "imported"
                row.source = _src
                row.source_detail = _src_detail
                # Record the ACTUAL delivered quality (read from the files), NOT the requested
                # target — Soulseek/SpotiFLAC often deliver CD-quality when hi-res was asked for,
                # and stamping the target caused false "Upgraded" badges on plain CD files.
                try:
                    _actual_q = _actual_flac_quality(moved_paths)
                    row.quality_acquired = _actual_q or (acq_entry.get("quality_acquired") if isinstance(acq_entry, dict) else None) or row.quality_acquired
                except Exception:
                    pass
                row.imported_paths = json.dumps(moved_paths)   # /Volumes/MediaVolume3/plexify-music paths, not /spotiflac
                # Teach the global dedup ledger every song we just imported, so it can never
                # be downloaded again (non-upgrading) from another album/compilation/source.
                try:
                    with SessionLocal() as _rs:
                        for _mp in moved_paths:
                            _la, _lt, _li, _lrk = _file_song_info(_mp)
                            _ledger_remember(_rs, _la or artist, _lt, _li,
                                             _lrk or _quality_rank(row.quality_acquired),
                                             path=_mp, quality=row.quality_acquired)
                        _rs.commit()
                except Exception:
                    log.exception("picker_tick: ledger remember failed (continuing)")
                # Dashboard: success → slides from "Right now" into "Recently added".
                # GOLD "Upgraded" badge ONLY for a genuine low-end → HIGH-END (hi-res) upgrade:
                # the album was already imported, the ACTUAL new file is hi-res, and it wasn't
                # hi-res before. A CD/lossless re-acquire is a success (green), not an upgrade.
                _outcome = "success"
                try:
                    if (_was_imported
                            and _quality_rank(row.quality_acquired) >= 3
                            and _quality_rank(_prev_quality) < 3):
                        _outcome = "upgrade"
                        row.was_upgraded = True
                    else:
                        row.was_upgraded = False
                except Exception:
                    pass
                try:
                    _record_outcome(row_id, _outcome, artist, album, _src, _src_detail)
                except Exception:
                    pass
            if not continue_to_finally:
                # Only finalize (size/note/mirror) for a real IMPORT — never for a row
                # we just re-queued (partial cleanup / all-moves-failed), whose files
                # were removed or never placed. Previously this ran unconditionally,
                # clobbering the requeue note and firing a needless Plex mirror.
                row.total_size_bytes = sum(
                    os.path.getsize(p) for p in moved_paths if os.path.exists(p)
                )
                _journey_str = " -> ".join(_journey_log) if _journey_log else "import"
                row.note = (
                    f"[{_journey_str}] "
                    f"files={len(moved_paths)} "
                    f"{row.total_size_bytes // 1024 // 1024}MB"
                )[:1024]
                # Trigger Plex playlist mirror
                try:
                    n = _trigger_mirror_for_imported_album(row)
                    out["mirror_triggered"] = n
                except Exception:
                    log.exception("picker_tick: mirror trigger failed (continuing)")
        else:
            error_str = res.error[:120] if res.error else "no files produced"
            _journey_str_fail = " -> ".join(_journey_log) if _journey_log else "no attempts"
            row.note = f"[{_journey_str_fail}] failed ({error_str})"[:1024]
            row.attempt_count = (row.attempt_count or 0) + 1
            # Reset status so next tick can retry (we pre-set 'downloading' on claim)
            row.status = "queued"
            # Dashboard: red fail → slides left + fades out of "Right now".
            try:
                _record_outcome(row_id, "fail", artist, album, _src, error_str)
            except Exception:
                pass

    set_config("autofill_last_picker_json", json.dumps(out))
    out["finished_at"] = datetime.utcnow().isoformat() + "Z"
    return out



# ====================================================================
# T36: discography refresh — periodic re-scan of each tracked artist's
# releases. Catches new albums, live tracks, singles since last scan.
# Only runs if mode == 'discography'.
# ====================================================================
def discography_refresh_tick() -> dict:
    out = {"started_at": datetime.utcnow().isoformat() + "Z",
           "checked_artists": 0, "new_album_rows": 0, "error": None}
    mode = (get_config("autofill_acquisition_mode", "album") or "album").lower()
    if mode != "discography":
        out["skipped"] = f"mode={mode} (not discography)"
        return out
    if (get_config("autofill_enabled", "0") or "0") != "1":
        out["skipped"] = "autofill disabled"
        return out

    from . import auth_spotify, spotify_client
    sp = auth_spotify.get_client()
    if not sp:
        out["error"] = "spotify not authed"
        return out

    # 1. Gather all artists currently in our local cache (LocalTrack)
    with session_scope() as s:
        track_ids = [r[0] for r in s.execute(
            select(LocalTrack.spotify_track_id).distinct()
        ).all() if r[0]]

    artist_ids: dict[str, str] = {}
    # Sampled: don't fetch 1000s of tracks, just enough to map artists
    for tid in track_ids[:300]:
        try:
            t = spotify_client._retry_patient(sp.track, tid)
        except Exception:
            continue
        if not t:
            continue
        a = (t.get("artists") or [{}])[0]
        if a.get("id"):
            artist_ids[a["id"]] = a.get("name") or ""
    out["checked_artists"] = len(artist_ids)
    log.info("discography_refresh: checking %d artists for new releases", len(artist_ids))

    # 2. For each artist, fetch artist_albums and queue any new ones
    new_count = 0
    with session_scope() as s:
        existing_keys = {(r[0], r[1]) for r in s.execute(
            select(AutofillAction.artist_key, AutofillAction.album_key)
        ).all()}

        for aid, aname in artist_ids.items():
            try:
                offset = 0
                while True:
                    page = spotify_client._retry_patient(
                        sp.artist_albums, aid,
                        album_type="album,single", limit=50, offset=offset,
                    )
                    items = (page or {}).get("items") or []
                    if not items:
                        break
                    for alb in items:
                        alb_name = alb.get("name") or ""
                        if not alb_name:
                            continue
                        ak = _normalize_for_key(aname)
                        bk = _normalize_for_key(alb_name)
                        if (ak, bk) in existing_keys:
                            continue
                        # New album — create AutofillAction
                        s.add(AutofillAction(
                            artist=aname, album=alb_name,
                            artist_key=ak, album_key=bk,
                            status="queued",
                            note=f"discography refresh: new release for {aname}",
                            pre_existing_files=0,
                        ))
                        existing_keys.add((ak, bk))
                        new_count += 1
                    if len(items) < 50:
                        break
                    offset += 50
                    if offset >= 200:
                        break
            except Exception:
                log.exception("discography_refresh: artist %s failed", aname)

    out["new_album_rows"] = new_count
    out["finished_at"] = datetime.utcnow().isoformat() + "Z"
    set_config("autofill_last_discography_refresh_json", json.dumps(out))
    log.info("discography_refresh: added %d new album rows", new_count)
    return out



# ====================================================================
# T37: mode-sync — when user changes acquisition mode, optionally prune
# files acquired by autofill that no longer match the new scope.
# Safe: moves to /Volumes/MediaVolume3/plexify-music/__autofill_pruned/{YYYY-MM-DD}/{artist}/{album}/
# (never deletes). User can rm whenever they're confident.
# ====================================================================
def apply_mode_to_library(*, dry_run: bool = True, new_mode: Optional[str] = None) -> dict:
    """Compute (and optionally apply) the prune set for switching to new_mode.

    Returns {target_mode, rows_total, rows_in_scope, rows_pruned, files_pruned,
             bytes_pruned, pruned_to_dir, samples}.
    """
    import os as _os
    out = {"target_mode": new_mode or get_config("autofill_acquisition_mode", "album"),
           "dry_run": dry_run,
           "rows_total": 0, "rows_in_scope": 0,
           "rows_pruned": 0, "files_pruned": 0, "bytes_pruned": 0,
           "pruned_to_dir": None, "samples": []}
    mode = (new_mode or get_config("autofill_acquisition_mode", "album") or "album").lower()
    if mode not in ("song", "album", "discography"):
        out["error"] = f"unknown mode {mode!r}"
        return out

    # Scope rules:
    #  song:  keep only files whose basename matches the EXACT spotify track title
    #         (per AutofillAction.track_ids_json + LocalTrack.title)
    #  album: keep all files from rows whose (artist, album) appear in LocalTrack
    #  discography: keep all files for ANY artist with ≥1 LocalTrack row
    from .db import LocalTrack
    with session_scope() as s:
        local_artists = {r[0] for r in s.execute(
            select(LocalTrack.artist).distinct()
        ).all() if r[0]}
        local_artist_album = {(r[0], r[1]) for r in s.execute(
            select(LocalTrack.artist, LocalTrack.album).distinct()
        ).all() if r[0]}
        # For song mode, build a map of (artist, album) -> set of expected track titles
        if mode == "song":
            local_titles_by_album: dict[tuple[str, str], set[str]] = {}
            for art, alb, title in s.execute(
                select(LocalTrack.artist, LocalTrack.album, LocalTrack.title)
            ).all():
                if not art:
                    continue
                key = (_normalize_for_key(art), _normalize_for_key(alb or ""))
                local_titles_by_album.setdefault(key, set()).add(_normalize_for_key(title or ""))

        rows = list(s.scalars(
            select(AutofillAction)
            .where(AutofillAction.imported_paths.isnot(None))
        ).all())

    out["rows_total"] = len(rows)
    pruned_dir_base = f"/Volumes/MediaVolume3/plexify-music/__autofill_pruned/{datetime.utcnow().strftime('%Y-%m-%d_%H%M')}"
    pruned_paths_log: list[dict] = []

    for r in rows:
        try:
            paths = json.loads(r.imported_paths or "[]")
        except Exception:
            paths = []
        if not paths:
            continue

        artist_key = r.artist_key or _normalize_for_key(r.artist)
        album_key = r.album_key or _normalize_for_key(r.album)

        # Determine scope for this row's files
        if mode == "discography":
            in_scope = (r.artist in local_artists) or any(
                _normalize_for_key(a) == artist_key for a in local_artists)
            files_in_scope = list(paths) if in_scope else []
            files_out = [] if in_scope else list(paths)
        elif mode == "album":
            in_scope = (artist_key, album_key) in {
                (_normalize_for_key(a), _normalize_for_key(b))
                for (a, b) in local_artist_album
            }
            files_in_scope = list(paths) if in_scope else []
            files_out = [] if in_scope else list(paths)
        elif mode == "song":
            wanted_titles = local_titles_by_album.get((artist_key, album_key), set())
            files_in_scope = []
            files_out = []
            for p in paths:
                base = _os.path.basename(p).rsplit(".", 1)[0]
                base_norm = _normalize_for_key(base)
                # Keep if ANY wanted title is contained in the basename
                if any(wt and wt in base_norm for wt in wanted_titles):
                    files_in_scope.append(p)
                else:
                    files_out.append(p)
        else:
            files_in_scope = list(paths)
            files_out = []

        if files_in_scope:
            out["rows_in_scope"] += 1
        if not files_out:
            continue

        out["rows_pruned"] += 1
        for p in files_out:
            if not _os.path.exists(p):
                continue
            try:
                sz = _os.path.getsize(p)
            except OSError:
                sz = 0
            out["bytes_pruned"] += sz
            out["files_pruned"] += 1
            if len(out["samples"]) < 8:
                out["samples"].append({"path": p, "size_mb": round(sz / 1024 / 1024, 1)})
            if not dry_run:
                # Move to __autofill_pruned/{date}/{relative-to-/Volumes/MediaVolume3/plexify-music}
                rel = _os.path.relpath(p, "/Volumes/MediaVolume3/plexify-music")
                dst = _os.path.join(pruned_dir_base, rel)
                _os.makedirs(_os.path.dirname(dst), exist_ok=True)
                try:
                    import shutil as _sh
                    _sh.move(p, dst)
                    pruned_paths_log.append({"from": p, "to": dst})
                except Exception as e:
                    log.warning("prune move failed: %s -> %s: %s", p, dst, e)

        if not dry_run:
            # Update the row to reflect pruning
            with session_scope() as s2:
                row2 = s2.get(AutofillAction, r.id)
                if row2:
                    if not files_in_scope:
                        row2.status = "pruned"
                        row2.imported_paths = None
                    else:
                        row2.imported_paths = json.dumps(files_in_scope)
                    row2.note = (row2.note or "") + f" [mode-sync→{mode}: pruned {len(files_out)} file(s)]"

    if not dry_run:
        out["pruned_to_dir"] = pruned_dir_base
        out["pruned_paths_log_count"] = len(pruned_paths_log)
        set_config("autofill_last_mode_sync_json", json.dumps({
            **out, "samples": out["samples"][:4],
        }))
    return out



# ====================================================================
# Orphan downloads sweep — runs at top of picker_tick. Catches files
# that slskd has delivered (via Lidarr autofill OR our picker OR a
# stalled prior tick) but never made it into /Volumes/MediaVolume3/plexify-music yet.
# ====================================================================
_SWEEP_LAST = 0.0
_SWEEP_MIN_INTERVAL = 120.0   # the sweep ran 2x per 8s tick x 5 workers — a full
                              # os.walk of /Volumes/MediaVolume3/Downloads/music EVERY time. Once per 2min
                              # process-wide is plenty (it only drains stragglers).
_SWEEP_WARNED: set = set()    # unattributable files already warned about (once per
                              # process, not 90k times — they sit there by design).


# ===== Stale-acquisition recovery (and shared rescue helpers) =====

# ===== Song-presence + locked-album helpers =====
# "The library is a mess": the same song kept landing as MULTIPLE files because
# every import path checked only exact FILENAMES (or a row's imported_paths
# JSON), while different sources name files differently ("01 - Title.flac" vs
# "Title - Artist.flac"). Presence must be decided by the song's TITLE (tag
# first, filename fallback) against what is ACTUALLY on disk.

def _norm_title_key(t: str) -> str:
    import re as _re
    t = t or ""
    # Feature credits are the SAME song, never a distinct version. Strip
    # "(feat. X)" / "[featuring X]" and a trailing "feat./ft. X" (stopping at
    # the next paren so a real "(Remix)"/"(Live)" marker survives). So "Monody"
    # and "Monody (feat. Laura Brehm)" collapse to one key, while remixes,
    # remasters, live and acoustic versions are deliberately preserved.
    t = _re.sub(r"[\(\[]\s*(?:feat|ft|featuring)\b[^)\]]*[\)\]]", " ", t, flags=_re.I)
    t = _re.sub(r"\s(?:feat|ft|featuring)\.?\s+[^(\[]*", " ", t, flags=_re.I)
    return _re.sub(r"[^a-z0-9]", "", t.lower())


def _file_title_key(path: str, artist_hint: str = "") -> str:
    """Normalized title key for an audio file: the TITLE tag when present,
    else derived from the filename (leading track numbers stripped; artist
    segments dropped). Version markers (remix/live/...) stay part of the key
    so genuinely different versions never collide."""
    import re as _re, os as _os
    try:
        from mutagen import File as _MF
        m = _MF(path, easy=True)
        t = (m.tags.get("title") or [""])[0] if m and m.tags else ""
        if t and t.strip():
            return _norm_title_key(t)
    except Exception:
        pass
    base = _os.path.splitext(_os.path.basename(path))[0]
    base = _re.sub(r"^\d{1,3}[\s.\-_)]+", "", base)
    if " - " in base:
        ah = _norm_title_key(artist_hint)
        parts = [seg.strip() for seg in base.split(" - ")]
        kept = [seg for seg in parts if _norm_title_key(seg) != ah] or parts
        # heuristics: drop a pure artist-list tail ("T - A, B" / "A - T")
        base = max(kept, key=len)
    return _norm_title_key(base)


def _folder_title_keys(folder: str, artist_hint: str = "") -> set:
    """Title keys of every audio file already in `folder` (disk truth).
    Corrupt-marked files don't count — their songs NEED re-downloading."""
    import os as _os
    AUDIO = (".flac", ".mp3", ".m4a", ".alac", ".aac", ".ogg", ".opus", ".wav")
    _corrupt = _corrupt_file_set()
    out = set()
    try:
        for fn in _os.listdir(folder):
            if _os.path.join(folder, fn) in _corrupt:
                continue
            if fn.lower().endswith(AUDIO):
                k = _file_title_key(_os.path.join(folder, fn), artist_hint)
                if k:
                    out.add(k)
    except OSError:
        pass
    return out


_LOCKED_DIRS_CACHE: tuple = (0.0, frozenset())

def _locked_album_dirs() -> frozenset:
    """Folders of albums in the FINAL, immutable 'complete_locked' stage.
    Nothing may write into these dirs; their songs are never re-downloaded,
    upgraded, or completed again. (10-min cache.)"""
    global _LOCKED_DIRS_CACHE
    ts, vals = _LOCKED_DIRS_CACHE
    if time.time() - ts < 600:
        return vals
    dirs = set()
    try:
        from .db import SessionLocal, AutofillAction
        with SessionLocal() as s_:
            for (ip,) in s_.query(AutofillAction.imported_paths).filter(
                    AutofillAction.status == "complete_locked").all():
                try:
                    paths = json.loads(ip or "[]")
                    if paths:
                        dirs.add(os.path.dirname(paths[0]))
                except Exception:
                    pass
    except Exception:
        log.exception("_locked_album_dirs failed")
    _LOCKED_DIRS_CACHE = (time.time(), frozenset(dirs))
    return _LOCKED_DIRS_CACHE[1]



def _log_recovery_move(kind: str, src: str, dst: str) -> None:
    """Append to the move ledger so bulk file recovery is reversible. Writes to
    <DATA_DIR>/acq_recovery_moves.jsonl — DATA_DIR, NOT a hardcoded /data, so the ledger
    actually exists in the Mac split (where /data is not a real path); rollback_acq_recovery.py
    replays it in reverse."""
    try:
        _led = os.path.join(os.environ.get("DATA_DIR", "/data"), "acq_recovery_moves.jsonl")
        with open(_led, "a") as fh:
            fh.write(json.dumps({"ts": time.time(), "kind": kind, "src": src, "dst": dst}) + "\n")
    except Exception:
        pass


_KNOWN_ARTISTS_CACHE: tuple = (0.0, frozenset())

def _known_artists() -> frozenset:
    """Lowercased artist names from the playlist mirror + autofill queue (1h cache).
    Used to validate 'Title - Artist' filename attribution — EXACT match only,
    because the user prefers a file sit in downloads forever over a wrong guess."""
    global _KNOWN_ARTISTS_CACHE
    ts, vals = _KNOWN_ARTISTS_CACHE
    if time.time() - ts < 3600 and vals:
        return vals
    import re as _re
    names = set()
    try:
        from .db import SessionLocal, LocalTrack, AutofillAction
        with SessionLocal() as s:
            for (a,) in s.execute(select(LocalTrack.artist).distinct()):
                if a and a.strip():
                    names.add(a.strip().lower())
                    for part in _re.split(r"\s*[,;]\s*|\s+feat\.?\s+|\s+ft\.?\s+|\s+&\s+", a, flags=_re.IGNORECASE):
                        if part and len(part.strip()) >= 2:
                            names.add(part.strip().lower())
            for (a,) in s.execute(select(AutofillAction.artist).distinct()):
                if a and a.strip():
                    names.add(a.strip().lower())
    except Exception:
        log.exception("_known_artists: lookup failed")
    if names:
        _KNOWN_ARTISTS_CACHE = (time.time(), frozenset(names))
    return _KNOWN_ARTISTS_CACHE[1]


_ACQ_RECOVER_LAST = 0.0
_ACQ_RECOVER_MIN_INTERVAL = 300.0   # one pass per 5 min
_ACQ_RECOVER_MIN_AGE_S = 3600.0     # never touch dirs younger than 1h (may be in-flight)

def recover_stale_acq_dirs(batch: int = 50, time_budget_s: float = 120.0) -> dict:
    """Rescue audio stranded in stale _acq_<row>_<ts> working dirs.

    The picker downloads each album into its own _acq_* dir and moves files out
    on success — but when the move failed (restart mid-acquisition, bad
    filename, full disk) the leftovers became INVISIBLE: the picker never
    revisits old dirs, and sweep_orphan_downloads deliberately skips _-prefixed
    paths (the quarantine convention). ~10,000 FLACs / 400 GB accumulated.

    Per stale dir (oldest first): ffmpeg-verify each audio file, attribute via
    tags (then 'Title - Artist' filename rescue against known artists), and move
    into /Volumes/MediaVolume3/plexify-music the way the sweep does. Corrupt files go to _attic_corrupt,
    files whose destination already exists go to _attic_dupes; nothing is
    deleted except truly empty dirs. Every move is logged to
    /data/acq_recovery_moves.jsonl for rollback. Batched + time-budgeted, and
    called BEHIND the picker's gates so it yields to video streaming."""
    global _ACQ_RECOVER_LAST
    out = {"recovered": 0, "dupes": 0, "corrupt": 0, "skipped_unattributable": 0,
           "removed_dirs": 0, "junk_dirs": 0, "bytes": 0}
    # OPT-IN: bulk file moves need explicit user enablement (set
    # acq_recovery_enabled=1 in app_config). Rollback ledger:
    # /data/acq_recovery_moves.jsonl + /data/rollback_acq_recovery.py
    if (get_config("acq_recovery_enabled", "0") or "0") != "1":
        out["disabled"] = True
        return out
    _now = time.time()
    if _now - _ACQ_RECOVER_LAST < _ACQ_RECOVER_MIN_INTERVAL:
        out["throttled"] = True
        return out
    _ACQ_RECOVER_LAST = _now

    import shutil as _sh
    try:
        from mutagen import File as MutagenFile
    except ImportError:
        MutagenFile = None
    AUDIO = (".flac", ".mp3", ".m4a", ".alac", ".aac", ".ogg", ".wav", ".opus")
    MUSIC = "/Volumes/MediaVolume3/plexify-music"
    deadline = _now + max(10.0, time_budget_s)
    moved = 0

    def _safe(name, default="Unknown"):
        name = (name or "").strip().rstrip(". ")
        for ch in ["/", "\\", "<", ">", ":", '"', "|", "?", "*"]:
            name = name.replace(ch, "-")
        return (name[:200] or default).strip(". ")

    for DL in ("/Volumes/MediaVolume3/Downloads/music/spotiflac", "/Volumes/MediaVolume3/Downloads/music/complete"):
        if not os.path.isdir(DL):
            continue
        try:
            acq_dirs = [os.path.join(DL, d) for d in os.listdir(DL)
                        if d.startswith("_acq_") and os.path.isdir(os.path.join(DL, d))]
        except OSError:
            continue
        acq_dirs.sort(key=lambda q: os.path.getmtime(q) if os.path.exists(q) else 0)
        for ad in acq_dirs:
            if moved >= batch or time.time() > deadline:
                out["partial"] = True
                return out
            try:
                if _now - os.path.getmtime(ad) < _ACQ_RECOVER_MIN_AGE_S:
                    continue
            except OSError:
                continue
            # The dirname carries the AutofillAction row id — the row knows the
            # REAL artist/album this acquisition was for. Authoritative when the
            # file's own tags are incomplete.
            row_artist = row_album = ""
            _m = __import__("re").match(r"_acq_(\d+)_", os.path.basename(ad))
            if _m:
                try:
                    from .db import SessionLocal as _SL, AutofillAction as _AA
                    with _SL() as _s2:
                        _row = _s2.get(_AA, int(_m.group(1)))
                        if _row:
                            row_artist = _row.artist or ""
                            row_album = _row.album or ""
                except Exception:
                    pass
            audio_files, other_files = [], 0
            for root, _d, files in os.walk(ad):
                for fn in files:
                    if fn.lower().endswith(AUDIO):
                        audio_files.append(os.path.join(root, fn))
                    else:
                        other_files += 1
            if not audio_files:
                # nothing to rescue: truly empty -> remove; junk-only (covers,
                # .part files) -> attic the whole dir, never delete.
                try:
                    if other_files == 0:
                        _sh.rmtree(ad)
                        out["removed_dirs"] += 1
                    else:
                        _attic = os.path.join(DL, "_attic_junk", os.path.basename(ad))
                        if not os.path.exists(_attic):
                            os.makedirs(os.path.dirname(_attic), exist_ok=True)
                            _sh.move(ad, _attic)
                            _log_recovery_move("junkdir", ad, _attic)
                            out["junk_dirs"] += 1
                except Exception:
                    pass
                continue
            for sp in audio_files:
                if moved >= batch or time.time() > deadline:
                    out["partial"] = True
                    break
                fn = os.path.basename(sp)
                artist, album = "", ""
                if MutagenFile:
                    try:
                        m = MutagenFile(sp, easy=True)
                        if m and m.tags:
                            artist = (m.tags.get("albumartist") or m.tags.get("artist") or [""])[0]
                            album = (m.tags.get("album") or [""])[0]
                    except Exception:
                        pass
                if not artist or not artist.strip():
                    _base = os.path.splitext(fn)[0]
                    if " - " in _base:
                        _l, _, _r = _base.rpartition(" - ")
                        _ka = _known_artists()
                        if _r.strip().lower() in _ka:
                            artist = _r.strip()
                        elif _l.strip().lower() in _ka:
                            artist = _l.strip()
                if not artist or not artist.strip():
                    artist = row_artist  # the queue row this dir was downloaded for
                if (not artist or not artist.strip() or artist.strip().lower() in
                        ("unknown", "unknown artist", "various", "va")):
                    out["skipped_unattributable"] += 1
                    continue
                if not album or not album.strip():
                    album = row_album
                if not album or not album.strip():
                    _b = os.path.splitext(fn)[0]
                    _tg = _b.rpartition(" - ")[0] if " - " in _b else _b
                    album = _album_from_db(_tg, artist)
                if not album or not album.strip():
                    # NO 'Unknown Album' imports — leave it; a later pass with
                    # better data (or the row) can still rescue it.
                    out.setdefault("skipped_no_album", 0)
                    out["skipped_no_album"] += 1
                    continue
                # INTENT GATE: only rescue songs that were asked for, or files
                # joining a folder the pipeline already built on purpose.
                _ik = _file_title_key(sp, artist)
                if _ik and _ik not in _intent_title_keys():
                    if not os.path.isdir(os.path.join(MUSIC, _safe(artist, ""), _safe(album, ""))):
                        out.setdefault("skipped_unrequested", 0)
                        out["skipped_unrequested"] += 1
                        continue
                # Integrity gate — a stranded file is often a truncated partial;
                # never import one silently.
                _intact, _bad = _verify_flac_integrity([sp])
                if _bad:
                    try:
                        _attic = os.path.join(DL, "_attic_corrupt", os.path.relpath(sp, DL))
                        os.makedirs(os.path.dirname(_attic), exist_ok=True)
                        _sh.move(sp, _attic)
                        _log_recovery_move("corrupt", sp, _attic)
                        out["corrupt"] += 1
                        moved += 1
                    except Exception:
                        pass
                    continue
                artist_s = _safe(artist, "")
                album_s = _safe(album, "")
                if not artist_s or not album_s:
                    out["skipped_unattributable"] += 1
                    continue
                dest_dir = os.path.join(MUSIC, artist_s, album_s)
                dp = os.path.join(dest_dir, fn)
                _tkey = _file_title_key(sp, artist_s)
                try:
                    if (os.path.exists(dp)
                            or dest_dir in _locked_album_dirs()
                            or (_tkey and _tkey in _folder_title_keys(dest_dir, artist_s))):
                        _attic = os.path.join(DL, "_attic_dupes", os.path.relpath(sp, DL))
                        os.makedirs(os.path.dirname(_attic), exist_ok=True)
                        _sh.move(sp, _attic)
                        _log_recovery_move("dupe", sp, _attic)
                        out["dupes"] += 1
                        moved += 1
                        continue
                    sz = os.path.getsize(sp)
                    os.makedirs(dest_dir, exist_ok=True)
                    os.chmod(dest_dir, 0o775)
                    try: os.chmod(os.path.dirname(dest_dir), 0o775)
                    except Exception: pass
                    _sh.move(sp, dp)
                    os.chmod(dp, 0o664)
                    try:
                        _stamp_file_tags(dp, artist_s, album_s)
                    except Exception:
                        pass
                    _log_recovery_move("import", sp, dp)
                    out["recovered"] += 1
                    out["bytes"] += sz
                    moved += 1
                except Exception as e:
                    out.setdefault("failed", 0)
                    out["failed"] += 1
                    log.warning("acq-recovery: %s -> %s failed: %s", sp, dp, e)
    return out



# ===== Plex-understanding verification (post-import) =====
# "It all depends on how Plex understands a new song or album — you can't change
# Plex's understanding, you have to cater to it." After every import the pipeline
# must CHECK which artist Plex actually filed the album under, compare with the
# file's albumartist tag, and correct Plex when they disagree.

_PLEX_VERIFY_SEEN: dict = {}      # album ratingKey -> consecutive mismatch count
_LOCAL_PATH_PREFIX = "/Volumes/MediaVolume3/plexify-music"    # this container's view of the music library (fixed by compose)

def _plex_prefix() -> str:
    """The music library path AS YOUR PLEX SERVER SEES IT (its mount of the same
    folder this app sees as /Volumes/MediaVolume3/plexify-music). Configurable per-setup in Settings ->
    Connections (config key plex_library_path) so the app is portable."""
    try:
        return (get_config("plex_library_path", _LOCAL_PATH_PREFIX) or _LOCAL_PATH_PREFIX).rstrip("/")
    except Exception:
        return _LOCAL_PATH_PREFIX

def _norm_artist_cmp(s: str) -> str:
    """Normalize an artist name for tag-vs-Plex comparison: casefold, unify
    dashes, treat all multi-artist separators alike, drop non-alphanumerics."""
    import re as _re, unicodedata as _ud
    s = _ud.normalize("NFKD", (s or ""))
    s = s.replace("\u2010", "-").replace("\u2011", "-")
    s = _re.sub(r"\s*(;|,|&|\bfeat\.?\b|\bft\.?\b|\bwith\b|\bx\b)\s*", "|", s, flags=_re.IGNORECASE)
    s = _re.sub(r"[^a-z0-9|]", "", s.casefold())
    # "Black Eyed Peas" == "The Black Eyed Peas"
    return "|".join(_re.sub(r"^the", "", part) for part in s.split("|"))


def _force_album_cover(srv_album) -> bool:
    """Force the album poster — the ONE thing we force on Plex. Grouping caters
    to Plex's understanding, but artwork must be the album's real cover (e.g.
    Hamilton filed under Lin-Manuel Miranda otherwise shows artist-derived art).

    Source order: the app's own Spotify album-art retrieval
    (download_album_cover — album's mapped Spotify track -> album images),
    then art embedded in the FLACs, then existing folder art. Whatever is found
    lands as cover.jpg in the album folder (so Plex's local agent also sees it)
    and is uploaded as the selected poster."""
    import glob as _glob
    from types import SimpleNamespace as _NS
    try:
        trs = srv_album.tracks()
        if not trs:
            return False
        loc = (trs[0].locations or [None])[0]
        if not loc:
            return False
        d = os.path.dirname(loc.replace(_plex_prefix(), _LOCAL_PATH_PREFIX, 1))
        if not os.path.isdir(d):
            return False
        cover = os.path.join(d, "cover.jpg")
        # 1) Spotify art via the existing retrieval tool: resolve a Spotify
        #    track id for any of this album's Plex tracks from the mapping table.
        if not os.path.isfile(cover):
            sid = None
            try:
                from .db import SessionLocal, TrackMapping
                keys = []
                for t in trs:
                    rk = str(getattr(t, "ratingKey", "") or "")
                    if rk:
                        keys += [rk, "/library/metadata/%s" % rk]
                with SessionLocal() as s_:
                    tm = s_.scalars(select(TrackMapping)
                                    .where(TrackMapping.plex_track_key.in_(keys))
                                    .where(TrackMapping.spotify_track_id.isnot(None))
                                    .limit(1)).first()
                    if tm:
                        sid = tm.spotify_track_id
            except Exception:
                pass
            if sid:
                try:
                    download_album_cover(_NS(id=0, track_ids_json=json.dumps([sid])), d)
                except Exception:
                    log.exception("_force_album_cover: spotify retrieval failed")
        # 2) art embedded in the album's own FLACs
        if not os.path.isfile(cover):
            from mutagen import File as _MF
            for f in sorted(_glob.glob(os.path.join(d, "*.flac")))[:5]:
                try:
                    pics = getattr(_MF(f), "pictures", []) or []
                    if pics:
                        with open(cover, "wb") as fh:
                            fh.write(pics[0].data)
                        break
                except Exception:
                    continue
        # 3) other folder art already present
        if not os.path.isfile(cover):
            for n in ("cover.png", "folder.jpg", "front.jpg"):
                fp = os.path.join(d, n)
                if os.path.isfile(fp):
                    cover = fp
                    break
        if not os.path.isfile(cover):
            return False
        srv_album.uploadPoster(filepath=cover)
        return True
    except Exception:
        log.exception("_force_album_cover failed for %r", getattr(srv_album, "title", "?"))
        return False



def plex_import_verify_tick(batch: int = 100, dance_limit: int = 3) -> dict:
    """Verify Plex's understanding of recently-added albums and cater to it.

    Phase 0 (always): finish any pending 'Plex dance' (move folders back into
    the library + rescan) recorded by a previous run — crash-safe via config.
    Phase 1: repair artist sort-title chimeras (artists whose title_sort says
    'Various Artists' while their title doesn't — leftover Plex-merge corruption
    that makes the scanner file NEW Various-Artists albums under random artists;
    this is exactly how a-ha kept swallowing soundtracks).
    Phase 2: for each recently-added album, read one track file's albumartist
    tag and compare with the artist Plex filed it under. A mismatch seen on two
    consecutive runs triggers the validated fix: dance the album folder out of
    the library, rescan (Plex drops the bad record), and the NEXT run dances it
    back + rescans so the (now sane) scanner re-files it correctly.
    Every move is appended to the rollback ledger."""
    import shutil as _sh
    out = {"checked": 0, "mismatch": 0, "danced_out": 0, "danced_back": 0,
           "sort_repaired": 0, "unreadable": 0}
    if (get_config("plex_verify_enabled", "1") or "1") != "1":
        out["skipped"] = "disabled"
        return out
    try:
        from . import plex_client
        from mutagen import File as MutagenFile
    except Exception:
        log.exception("plex_import_verify_tick: imports failed")
        return out
    srv = plex_client._connect()
    if not srv:
        out["skipped"] = "plex unreachable"
        return out
    sec = plex_client._music_section(srv)
    if not sec:
        out["skipped"] = "no music section"
        return out
    fix_enabled = (get_config("plex_verify_fix_enabled", "1") or "1") == "1"
    DANCE_DIR = "/Volumes/MediaVolume3/Downloads/music/_plexdance"

    def _report(rec: dict) -> None:
        try:
            rec["ts"] = time.time()
            with open("/data/plex_understanding.jsonl", "a") as fh:
                fh.write(json.dumps(rec) + "\n")
        except Exception:
            pass

    # ── Phase 0: finish pending dances (move back + rescan), crash-safe.
    try:
        pending = json.loads(get_config("plex_dance_pending", "[]") or "[]")
    except Exception:
        pending = []
    still_pending = []
    for d in pending:
        src = os.path.join(DANCE_DIR, d["name"])
        dst = d["folder"]
        try:
            if os.path.isdir(src) and not os.path.exists(dst):
                _sh.move(src, dst)
                _log_recovery_move("dance_back", src, dst)
                sec.update(path=os.path.dirname(dst.replace(_LOCAL_PATH_PREFIX, _plex_prefix())))
                out["danced_back"] += 1
                _report({"action": "dance_back", "album": d.get("title"), "folder": dst})
                # rebuilt albums lose any selected poster — queue a cover-force
                try:
                    _cp = json.loads(get_config("plex_cover_pending", "[]") or "[]")
                except Exception:
                    _cp = []
                _cp.append({"title": d.get("title") or "", "folder": dst, "tries": 0})
                set_config("plex_cover_pending", json.dumps(_cp[-50:]))
            elif os.path.isdir(src):
                still_pending.append(d)  # destination reappeared?! don't clobber
                log.warning("plex_verify: dance-back blocked, %s already exists", dst)
        except Exception:
            log.exception("plex_verify: dance-back failed for %s", dst)
            still_pending.append(d)
    if pending != still_pending:
        set_config("plex_dance_pending", json.dumps(still_pending))
    if still_pending:
        # let the moved-back folders settle before judging anything else
        return out

    # ── Phase 1: artist sort-title chimera repair (the root-cause guard).
    try:
        for art in sec.search(libtype="artist", **{"artist.title!=": "Various Artists"}):
            ts_ = (getattr(art, "titleSort", "") or "")
            if ts_ == "Various Artists" and art.title != "Various Artists":
                if fix_enabled:
                    try:
                        art.editSortTitle(art.title, locked=False)
                        out["sort_repaired"] += 1
                        log.warning("plex_verify: repaired sort-title chimera on %r (was 'Various Artists')",
                                    art.title)
                        _report({"action": "sort_repair", "artist": art.title, "rk": art.ratingKey})
                    except Exception:
                        log.exception("plex_verify: sort repair failed for %r", art.title)
                else:
                    _report({"action": "sort_chimera_found", "artist": art.title, "rk": art.ratingKey})
    except Exception:
        # filter form unsupported on some PMS versions — fall back to skip
        log.debug("plex_verify: artist chimera scan unavailable", exc_info=True)

    # ── Phase 2: recently-added albums — does Plex's artist match the tags?
    try:
        recent = sec.recentlyAdded(libtype="album", maxresults=max(10, batch))
    except Exception:
        log.exception("plex_verify: recentlyAdded failed")
        return out
    dances = 0
    new_pending = []
    for al in recent:
        rk = al.ratingKey
        plex_artist = al.parentTitle or ""
        try:
            trs = al.tracks()
        except Exception:
            continue
        if not trs:
            continue
        loc = (trs[0].locations or [None])[0]
        if not loc or not loc.startswith(_plex_prefix()):
            continue
        lpath = loc.replace(_plex_prefix(), _LOCAL_PATH_PREFIX, 1)
        if not os.path.isfile(lpath):
            continue
        tag_aa = ""
        try:
            m = MutagenFile(lpath, easy=True)
            if m and m.tags:
                tag_aa = (m.tags.get("albumartist") or m.tags.get("artist") or [""])[0]
        except Exception:
            out["unreadable"] += 1
            continue
        if not tag_aa.strip():
            continue
        out["checked"] += 1
        # Plex's '[Unknown Album]' placeholder = it indexed the files before
        # their tags were written and never re-read them. Treat as a mismatch
        # (regardless of artist agreement) so the dance rebuilds it from tags.
        _ua_ghost = "unknown album" in (al.title or "").lower()
        want, got = _norm_artist_cmp(tag_aa), _norm_artist_cmp(plex_artist)
        if not _ua_ghost and (not want or want == got):
            _PLEX_VERIFY_SEEN.pop(rk, None)
            continue
        # Plex split/merged multi-artist credits differently — any shared
        # constituent counts as agreement (catering to Plex's understanding).
        if not _ua_ghost and set(want.split("|")) & set(got.split("|")):
            _PLEX_VERIFY_SEEN.pop(rk, None)
            continue
        out["mismatch"] += 1
        n = _PLEX_VERIFY_SEEN.get(rk, 0) + 1
        _PLEX_VERIFY_SEEN[rk] = n
        log.warning("plex_verify: %r filed under %r but tagged %r (sighting %d)",
                    al.title, plex_artist, tag_aa, n)
        _report({"action": "mismatch", "album": al.title, "plex_artist": plex_artist,
                 "tag_albumartist": tag_aa, "rk": rk, "sighting": n})
        # Auto-correct on the 2nd consecutive sighting (gives Plex's async agent
        # one interval to settle), bounded per run, single-folder albums only.
        if not fix_enabled or n < 2 or dances >= max(0, dance_limit):
            continue
        try:
            _hist = set(json.loads(get_config("plex_dance_history", "[]") or "[]"))
        except Exception:
            _hist = set()
        folders = {os.path.dirname((t.locations or [""])[0]) for t in trs}
        folders.discard("")
        if len(folders) != 1:
            continue
        folder = folders.pop().replace(_plex_prefix(), _LOCAL_PATH_PREFIX, 1)
        if not folder.startswith(_LOCAL_PATH_PREFIX) or not os.path.isdir(folder):
            continue
        if folder in _hist:
            # Already danced once and Plex re-filed it the same way — that IS
            # Plex's understanding. Stop fighting; keep it on the report only.
            continue
        try:
            os.makedirs(DANCE_DIR, exist_ok=True)
            dst = os.path.join(DANCE_DIR, os.path.basename(folder))
            if os.path.exists(dst):
                continue
            _sh.move(folder, dst)
            _log_recovery_move("dance_out", folder, dst)
            sec.update(path=os.path.dirname(folder.replace(_LOCAL_PATH_PREFIX, _plex_prefix())))
            new_pending.append({"name": os.path.basename(folder), "folder": folder, "title": al.title})
            _hist.add(folder)
            set_config("plex_dance_history", json.dumps(sorted(_hist)[-500:]))
            dances += 1
            out["danced_out"] += 1
            _PLEX_VERIFY_SEEN.pop(rk, None)
            _report({"action": "dance_out", "album": al.title, "plex_artist": plex_artist,
                     "tag_albumartist": tag_aa, "folder": folder})
            log.warning("plex_verify: danced out %r (Plex said %r, tags say %r) — will dance back next run",
                        al.title, plex_artist, tag_aa)
        except Exception:
            log.exception("plex_verify: dance-out failed for %r", al.title)
    if new_pending:
        try:
            cur = json.loads(get_config("plex_dance_pending", "[]") or "[]")
        except Exception:
            cur = []
        set_config("plex_dance_pending", json.dumps(cur + new_pending))

    # ── Phase 2b: library-wide ARTIST DRIFT repair (from the hygiene scan's
    # folder<->album map) — folders whose Plex artist contradicts their parent
    # dir, beyond the recently-added window. Danced like any mismatch.
    if fix_enabled:
        try:
            drift = json.load(open("/data/artist_drift.json"))
        except Exception:
            drift = []
        if drift:
            try:
                _hist2 = set(json.loads(get_config("plex_dance_history", "[]") or "[]"))
            except Exception:
                _hist2 = set()
            danced2 = 0
            remaining = []
            import shutil as _shd
            for d in drift:
                folder = d.get("folder") or ""
                if danced2 >= 3 or not os.path.isdir(folder) or folder in _hist2:
                    if os.path.isdir(folder) and folder not in _hist2:
                        remaining.append(d)
                    continue
                try:
                    os.makedirs("/Volumes/MediaVolume3/Downloads/music/_plexdance", exist_ok=True)
                    dst = os.path.join("/Volumes/MediaVolume3/Downloads/music/_plexdance", os.path.basename(folder))
                    if os.path.exists(dst):
                        continue
                    _shd.move(folder, dst)
                    _log_recovery_move("dance_out", folder, dst)
                    sec.update(path=os.path.dirname(folder.replace(_LOCAL_PATH_PREFIX, _plex_prefix())))
                    new_pending.append({"name": os.path.basename(folder), "folder": folder,
                                        "title": os.path.basename(folder)})
                    _hist2.add(folder)
                    danced2 += 1
                    out.setdefault("drift_danced", 0)
                    out["drift_danced"] += 1
                    log.warning("plex_verify: drift dance %r (Plex said %r, dir says %r)",
                                os.path.basename(folder), d.get("plex_artist"), d.get("dir_artist"))
                except Exception:
                    log.exception("plex_verify: drift dance failed for %s", folder)
            if danced2:
                set_config("plex_dance_history", json.dumps(sorted(_hist2)[-500:]))
                json.dump(remaining + [x for x in drift if x.get("folder") in _hist2 and os.path.isdir(x.get("folder", ""))][:0], open("/data/artist_drift.json", "w"))

    # ── Phase 3: SPLIT-ALBUM check — the same album existing under several
    # artists (e.g. tick, tick... BOOM! under Various Artists + 'Soundtracks' +
    # a cast name) because different acquisition paths invented different artist
    # folders/tags. For each recently-added album, look for same-title albums
    # under OTHER artists; consolidate the smaller tile into the bigger one:
    # move files into the canonical folder and copy the canonical file's EXACT
    # albumartist tag values (multi-value aware — Plex treats ['A','B'] and
    # 'A;B' as different artists). Exact-dupe tracks go to the attic.
    import re as _re3
    def _ntitle3(t):
        t = _re3.sub(r"\[[^\]]*\]", "", t or "")
        return _re3.sub(r"[^a-z0-9]", "", t.lower())
    _SOUNDTRACKY = _re3.compile(r"soundtrack|motion picture|original cast|musical|broadway", _re3.I)
    _VAISH = _re3.compile(r"various artists|soundtracks?$|the cast|^cast\b", _re3.I)
    out["splits"] = 0
    consolidations = 0
    seen_titles = set()

    # Cover queue: albums rebuilt by a dance need their real cover re-forced
    # once the scanner has re-created them.
    try:
        _cp = json.loads(get_config("plex_cover_pending", "[]") or "[]")
    except Exception:
        _cp = []
    if _cp:
        _cp_left = []
        for d in _cp:
            forced = False
            try:
                for a in sec.search(title=d.get("title") or "", libtype="album"):
                    trs = a.tracks()
                    loc = (trs[0].locations or [None])[0] if trs else None
                    if loc and os.path.dirname(loc.replace(_plex_prefix(), _LOCAL_PATH_PREFIX, 1)) == d.get("folder"):
                        forced = _force_album_cover(a)
                        break
            except Exception:
                pass
            if forced:
                out.setdefault("covers_forced", 0)
                out["covers_forced"] += 1
                _report({"action": "cover_forced", "album": d.get("title"), "folder": d.get("folder")})
            else:
                d["tries"] = (d.get("tries") or 0) + 1
                if d["tries"] < 5:
                    _cp_left.append(d)
        set_config("plex_cover_pending", json.dumps(_cp_left))

    for al in recent:
        try:
            tkey = _ntitle3(al.title)
            if not tkey or tkey in seen_titles:
                continue
            seen_titles.add(tkey)
            try:
                same = [a for a in sec.search(title=al.title, libtype="album")
                        if _ntitle3(a.title) == tkey]
            except Exception:
                continue
            tiles = {}
            by_artist = {}
            for a in same:
                tiles.setdefault(a.parentRatingKey, a)
                by_artist.setdefault(a.parentRatingKey, []).append(a)
            # Same-artist duplicate tiles (e.g. two 'Hamilton' albums both under
            # Lin-Manuel Miranda after a consolidation): merge into the largest
            # via Plex's own merge API, then force the real cover.
            for prk, albs in by_artist.items():
                if len(albs) < 2 or not fix_enabled:
                    continue
                albs.sort(key=lambda a: -(len(a.tracks() or [])))
                big, rest = albs[0], albs[1:]
                try:
                    srv.query("/library/metadata/%s/merge?ids=%s" % (
                        big.ratingKey, ",".join(str(a.ratingKey) for a in rest)),
                        method=srv._session.put)
                    out.setdefault("tile_merges", 0)
                    out["tile_merges"] += 1
                    _report({"action": "tile_merge", "album": big.title,
                             "artist": big.parentTitle, "into": big.ratingKey,
                             "merged": [a.ratingKey for a in rest]})
                    log.warning("plex_verify: merged %d duplicate %r tiles under %r",
                                len(rest), big.title, big.parentTitle)
                    big.reload()
                    tiles[prk] = big  # merged-away siblings are gone — keep the survivor
                    if _force_album_cover(big):
                        out.setdefault("covers_forced", 0)
                        out["covers_forced"] += 1
                except Exception:
                    log.exception("plex_verify: tile merge failed for %r", big.title)
            if len(tiles) < 2:
                continue
            def _ntr(a):
                try:
                    return len(a.tracks() or [])
                except Exception:
                    return 0  # tile vanished mid-run (merged/deleted)
            group = sorted(tiles.values(), key=lambda a: -_ntr(a))
            # SAME FOLDER, different artists = one album the agent split between
            # two artist records (e.g. Culture Wars tracks matched to Culture
            # Club). Objective — merge regardless of any signal.
            try:
                folders = set()
                for a in group:
                    trs2 = a.tracks()
                    if trs2 and (trs2[0].locations or [None])[0]:
                        folders.add(os.path.dirname(trs2[0].locations[0]))
                if len(folders) == 1 and len(group) > 1:
                    big2, rest2 = group[0], group[1:]
                    srv.query("/library/metadata/%s/merge?ids=%s" % (
                        big2.ratingKey, ",".join(str(x.ratingKey) for x in rest2)),
                        method=srv._session.put)
                    out.setdefault("tile_merges", 0)
                    out["tile_merges"] += 1
                    log.warning("plex_verify: merged same-folder split %r (%s)",
                                al.title, ", ".join((x.parentTitle or "?")[:24] for x in group))
                    big2.reload()
                    _force_album_cover(big2)
                    continue
            except Exception:
                log.exception("plex_verify: same-folder merge failed for %r", al.title)
            signal = _SOUNDTRACKY.search(al.title or "") or any(
                _VAISH.search(a.parentTitle or "") for a in group)
            if not signal:
                continue  # same-named albums by different real artists are legit
            out["splits"] += 1
            canon, strays = group[0], group[1:]
            _report({"action": "split_album", "album": al.title,
                     "artists": [a.parentTitle for a in group],
                     "canonical": canon.parentTitle})
            log.warning("plex_verify: split album %r across %d artists (%s) — canonical %r",
                        al.title, len(group), ", ".join((a.parentTitle or "?")[:28] for a in group),
                        canon.parentTitle)
            if not fix_enabled or consolidations >= 2:
                continue
            cfiles = [l for t in canon.tracks() for l in (t.locations or [])]
            if not cfiles:
                continue
            canon_dir = os.path.dirname(cfiles[0]).replace(_plex_prefix(), _LOCAL_PATH_PREFIX, 1)
            if not os.path.isdir(canon_dir):
                continue
            try:
                _hist = set(json.loads(get_config("plex_dance_history", "[]") or "[]"))
            except Exception:
                _hist = set()
            ref_aa = None
            try:
                _rm = MutagenFile(os.path.join(canon_dir, os.path.basename(cfiles[0])))
                ref_aa = list(_rm.get("albumartist") or _rm.get("ALBUMARTIST") or []) or None
            except Exception:
                pass
            canon_titles = {_ntitle3(os.path.splitext(os.path.basename(f))[0]) for f in cfiles}
            import shutil as _sh3
            did = False
            for stray in strays:
                for t in stray.tracks():
                    loc = (t.locations or [None])[0]
                    if not loc:
                        continue
                    lf = loc.replace(_plex_prefix(), _LOCAL_PATH_PREFIX, 1)
                    if not os.path.isfile(lf) or os.path.dirname(lf) == canon_dir or lf in _hist:
                        continue
                    fn = os.path.basename(lf)
                    try:
                        if _ntitle3(os.path.splitext(fn)[0]) in canon_titles or \
                                os.path.exists(os.path.join(canon_dir, fn)):
                            adst = os.path.join("/Volumes/MediaVolume3/Downloads/music/_attic_dupes/split_albums",
                                                os.path.relpath(lf, _LOCAL_PATH_PREFIX))
                            os.makedirs(os.path.dirname(adst), exist_ok=True)
                            _sh3.move(lf, adst)
                            _log_recovery_move("dupe", lf, adst)
                        else:
                            dst = os.path.join(canon_dir, fn)
                            _sh3.move(lf, dst)
                            _log_recovery_move("consolidate", lf, dst)
                            try:
                                m = MutagenFile(dst, easy=True)
                                if ref_aa:
                                    m["albumartist"] = ref_aa
                                m["album"] = canon.title
                                m.save()
                            except Exception:
                                pass
                            canon_titles.add(_ntitle3(os.path.splitext(fn)[0]))
                        _hist.add(lf)
                        did = True
                        sec.update(path=os.path.dirname(loc))
                    except Exception:
                        log.exception("plex_verify: consolidate failed for %s", lf)
            if did:
                consolidations += 1
                out.setdefault("consolidated", 0)
                out["consolidated"] += 1
                set_config("plex_dance_history", json.dumps(sorted(_hist)[-500:]))
                sec.update(path=os.path.dirname(cfiles[0]))
                if _force_album_cover(canon):
                    out.setdefault("covers_forced", 0)
                    out["covers_forced"] += 1
                _report({"action": "consolidated", "album": al.title,
                         "into": canon.parentTitle})
                log.warning("plex_verify: consolidated split album %r into %r",
                            al.title, canon.parentTitle)
        except Exception:
            log.exception("plex_verify: split check failed for %r", getattr(al, "title", "?"))

    # ── Phase 3b: LIBRARY-WIDE same-artist duplicate tiles. Phase 3 only sees
    # recently-added albums, so tiles that split during heavy import churn fall
    # out of its window (e.g. 15 single-track 'Elvis%27 Golden Records' tiles).
    # Walk the whole album list at most once per hour, merge same-artist
    # same-title tiles into the largest (capped per run).
    try:
        _last = float(get_config("tile_sweep_last", "0") or "0")
    except Exception:
        _last = 0.0
    if fix_enabled and time.time() - _last > 3600:
        set_config("tile_sweep_last", str(time.time()))
        try:
            import re as _re3b
            def _nt(t):
                t = _re3b.sub(r"\[[^\]]*\]", "", t or "")
                return _re3b.sub(r"[^a-z0-9]", "", t.lower())
            groups = {}
            for a in sec.albums():
                k = (a.parentRatingKey, _nt(a.title))
                if k[1]:
                    groups.setdefault(k, []).append(a)
            merges = 0
            for (prk, _t), albs in groups.items():
                if len(albs) < 2 or merges >= 12:
                    continue
                def _ntr2(x):
                    try:
                        return len(x.tracks() or [])
                    except Exception:
                        return 0
                albs.sort(key=lambda x: -_ntr2(x))
                big, rest = albs[0], [x for x in albs[1:] if _ntr2(x) >= 0]
                try:
                    srv.query("/library/metadata/%s/merge?ids=%s" % (
                        big.ratingKey, ",".join(str(x.ratingKey) for x in rest)),
                        method=srv._session.put)
                    merges += 1
                    out.setdefault("tile_merges", 0)
                    out["tile_merges"] += 1
                    log.warning("plex_verify: library sweep merged %d duplicate %r tiles under %r",
                                len(rest), big.title, big.parentTitle)
                    big.reload()
                    if _force_album_cover(big):
                        out.setdefault("covers_forced", 0)
                        out["covers_forced"] += 1
                except Exception:
                    log.exception("plex_verify: library tile merge failed for %r", big.title)
            if merges:
                _report({"action": "library_tile_sweep", "merged_groups": merges})
        except Exception:
            log.exception("plex_verify: library tile sweep failed")

    if out["mismatch"] or out["sort_repaired"] or out["danced_out"] or out["danced_back"] \
            or out["splits"] or out.get("tile_merges"):
        log.info("plex_import_verify_tick: %s", out)
    return out



def _album_from_db(title: str, artist: str) -> str:
    """Resolve an album name for a loose track from the local Spotify mirror
    (liked-tracks cache, then playlist mirror). Exact casefold match on
    title+artist — used so single-file imports land in their REAL album folder
    instead of 'Unknown Album'."""
    t, a = (title or "").strip().casefold(), (artist or "").strip().casefold()
    if not t or not a:
        return ""
    try:
        from .db import SessionLocal, SpotifyLikedTrack, LocalTrack
        with SessionLocal() as s_:
            row = (s_.query(SpotifyLikedTrack)
                   .filter(SpotifyLikedTrack.title.ilike(title.strip()),
                           SpotifyLikedTrack.artist.ilike(artist.strip() + "%"))
                   .first())
            if row and getattr(row, "album", ""):
                return row.album or ""
            lt = (s_.query(LocalTrack)
                  .filter(LocalTrack.title.ilike(title.strip()),
                          LocalTrack.artist.ilike(artist.strip() + "%"))
                  .first())
            if lt and getattr(lt, "album", ""):
                return lt.album or ""
    except Exception:
        pass
    return ""



def cover_identity_tick(id_batch: int = 80, cover_batch: int = 15, merge_batch: int = 3) -> dict:
    """Album identity = its COVER (the Spotify album it came from), not its
    artist/name strings.

    Phase A — every file-owning row gets its identity: foreign_album_id (the
    Spotify album id, resolved from the row's tracks via the local mirror) and
    cover_url (that album's cover image).
    Phase B — every album folder gets its assigned cover written as cover.jpg.
    Phase C — rows that share the same cover identity are THE SAME ALBUM:
    their folders are recombined (same-song dupes -> attic), the extra rows
    superseded — regardless of how the artist or album name was spelled when
    they were queued.
    """
    import shutil as _sh
    out = {"ids_assigned": 0, "covers_assigned": 0, "merged": 0, "skipped": None}
    if (get_config("acq_recovery_enabled", "0") or "0") != "1":
        out["skipped"] = "disabled"
        return out
    # Phases A (identity, DB-only) and B (small cover JPEGs) never compete with
    # video playback — only Phase C (file moves) yields to streaming.
    _streaming = False
    try:
        _streaming = _plex_active_video_sessions() > 0
    except Exception:
        pass
    from .db import SessionLocal, AutofillAction, SpotifyAlbum, SpotifyAlbumTrack
    ACTIVE = ("imported", "complete_locked", "library_existing", "queued")
    OWNING = ("imported", "complete_locked")

    # ── Phase A: identity backfill — local mirror first (biggest containing
    # album), then Spotify's batched tracks endpoint for the rest. Cursor-paged
    # so unresolvable rows can never clog the batch.
    try:
        _cur = int(get_config("cover_id_cursor", "0") or "0")
    except Exception:
        _cur = 0
    with SessionLocal() as s_:
        track_to_album = {}
        rows = (s_.query(AutofillAction)
                .filter(AutofillAction.status.in_(ACTIVE))
                .filter(AutofillAction.id > _cur)
                .filter((AutofillAction.foreign_album_id.is_(None))
                        | (AutofillAction.foreign_album_id == "")
                        | (AutofillAction.cover_url.is_(None))
                        | (AutofillAction.cover_url == ""))
                .order_by(AutofillAction.id)
                .limit(id_batch).all())
        set_config("cover_id_cursor", str(rows[-1].id) if rows else "0")
        out["scanned"] = len(rows)
        # collect unresolved-by-mirror rows for one batched API sweep at the end
        _api_rows = []
        img_by_album = {}
        _api_fetches = [0]
        for r in rows:
            aid = (r.foreign_album_id or "")[3:] if (r.foreign_album_id or "").startswith("sp:") else None
            if not aid:
                try:
                    tids = json.loads(r.track_ids_json or "[]") or []
                except Exception:
                    tids = []
                for tid in tids[:5]:
                    if tid in track_to_album:
                        aid = track_to_album[tid]
                        break
                    # the queue policy is "the BIGGEST album containing the liked
                    # song" — so when several mirrored albums contain this track,
                    # the identity (and its cover) is the biggest one's.
                    cands = (s_.query(SpotifyAlbumTrack, SpotifyAlbum)
                             .join(SpotifyAlbum, SpotifyAlbum.album_id == SpotifyAlbumTrack.album_id)
                             .filter(SpotifyAlbumTrack.track_id == tid).all())
                    if cands:
                        # the album the row NAMES wins; biggest containing album
                        # only as fallback — never let a single's identity (and
                        # cover) attach to a row that names the big edition.
                        nm = [c for c in cands if _album_names_match(c[1].name, r.album or "")]
                        # RULE 4: the biggest-album fallback never elects a >50
                        # counterpart as a row's identity.
                        small = [c for c in cands if int(c[1].total_tracks or 0) <= 50]
                        best = max(nm or small or cands, key=lambda c: int(c[1].total_tracks or 0))
                        aid = best[1].album_id
                        track_to_album[tid] = aid
                        break
                if aid:
                    r.foreign_album_id = "sp:" + aid
                    out["ids_assigned"] += 1
                elif tids:
                    _api_rows.append((r, tids[0]))
            if aid and not (r.cover_url or "").strip():
                if aid not in img_by_album:
                    alb = s_.get(SpotifyAlbum, aid)
                    img = (alb.image_url or "") if alb else ""
                    if not img and _api_fetches[0] < 10:
                        # mirror hasn't stored the image yet — it's one easy
                        # Spotify call away (fail-fast, capped per run)
                        try:
                            from . import auth_spotify as _asp
                            _sp = _asp.get_client()
                            a = _sp.album(aid) if _sp else None
                            img = ((a.get("images") or [{}])[0].get("url") or "") if a else ""
                            _api_fetches[0] += 1
                            if img and alb:
                                alb.image_url = img[:512]
                        except Exception:
                            pass
                    img_by_album[aid] = img
                if img_by_album[aid]:
                    r.cover_url = img_by_album[aid]
        # Batched Spotify fallback: tracks the mirror doesn't know — 50 ids per
        # call, a handful of calls per run. The track's album gives both the
        # identity and the cover in one shot.
        if _api_rows:
            try:
                from . import auth_spotify as _asp2
                _sp2 = _asp2.get_client()
            except Exception:
                _sp2 = None
            if _sp2:
                # NOTE: the batched /v1/tracks endpoint 403s for dev-mode apps —
                # single track lookups still work, so use those (small per-run cap).
                for r, tid in _api_rows[:25]:
                    try:
                        t = _sp2.track(tid)
                    except Exception:
                        log.warning("cover_identity: track call failed; stopping API sweep")
                        break
                    alb = (t.get("album") or {}) if t else {}
                    aid2 = alb.get("id")
                    if not aid2:
                        continue
                    if (r.album or "").strip() and not _album_names_match(alb.get("name", ""), r.album):
                        continue  # track's default album != the album this row names
                    r.foreign_album_id = "sp:" + aid2
                    img = ((alb.get("images") or [{}])[0].get("url") or "")
                    if img:
                        r.cover_url = img[:512]
                    out["ids_assigned"] += 1
        s_.commit()

    # ── Phase B: write each album folder's assigned cover.jpg ──
    import requests as _rq
    with SessionLocal() as s_:
        rows = (s_.query(AutofillAction)
                .filter(AutofillAction.status.in_(OWNING),
                        AutofillAction.cover_url.isnot(None),
                        AutofillAction.imported_paths.isnot(None)).all())
        n = 0
        for r in rows:
            if n >= cover_batch:
                break
            try:
                paths = json.loads(r.imported_paths or "[]") or []
            except Exception:
                continue
            if not paths:
                continue
            fold = os.path.dirname(paths[0])
            dest = os.path.join(fold, "cover.jpg")
            marker = os.path.join(fold, ".plexify_placeholder")
            if not os.path.isdir(fold) or (os.path.exists(dest) and not os.path.exists(marker)):
                continue
            try:
                resp = _rq.get(r.cover_url, timeout=8)
                if resp.status_code == 200 and resp.content:
                    with open(dest, "wb") as fh:
                        fh.write(resp.content)
                    os.chmod(dest, 0o664)
                    try:
                        os.remove(marker)   # the fake cover is now the real one
                    except OSError:
                        pass
                    out["covers_assigned"] += 1
                    n += 1
            except Exception:
                pass

    # ── Phase C: same cover identity = same album -> combine ──
    if _streaming:
        out["skipped"] = "streaming (merge phase only)"
        if out["ids_assigned"] or out["covers_assigned"]:
            log.info("cover_identity_tick: %s", out)
        return out
    merged_dirs = []
    with SessionLocal() as s_:
        groups: dict = {}
        for r in s_.query(AutofillAction).filter(
                AutofillAction.status.in_(OWNING),
                AutofillAction.imported_paths.isnot(None)).all():
            # RULE 1: identical cover IMAGE combines albums (even across
            # artists / album ids — deluxe and standard share one cover).
            key = ("cu:" + r.cover_url) if (r.cover_url or "").strip() else (
                r.foreign_album_id if (r.foreign_album_id or "").startswith("sp:") else None)
            if key:
                groups.setdefault(key, []).append(r)
        for fa, rows in groups.items():
            # distinct folders only — same-folder rows are just bookkeeping dupes
            by_dir = {}
            for r in rows:
                try:
                    paths = json.loads(r.imported_paths or "[]") or []
                except Exception:
                    paths = []
                if paths:
                    by_dir.setdefault(os.path.dirname(paths[0]), []).append(r)
            live = {d: rs for d, rs in by_dir.items() if os.path.isdir(d)}
            if len(live) < 2 or out["merged"] >= merge_batch:
                continue
            # canonical home: a locked row's folder wins, else the fullest folder
            def _naudio(d):
                try:
                    return sum(1 for f in os.listdir(d) if f.lower().endswith(
                        (".flac", ".mp3", ".m4a", ".alac", ".ogg", ".opus")))
                except OSError:
                    return 0
            locked_dirs = [d for d, rs in live.items() if any(x.status == "complete_locked" for x in rs)]
            canon_dir = locked_dirs[0] if locked_dirs else max(live, key=_naudio)
            canon_rows = live[canon_dir]
            dtitles = _folder_title_keys(canon_dir)
            moved = 0
            for d, rs in live.items():
                if d == canon_dir:
                    continue
                for fn in list(os.listdir(d)):
                    sp = os.path.join(d, fn)
                    if not fn.lower().endswith((".flac", ".mp3", ".m4a", ".alac", ".ogg", ".opus")):
                        continue
                    tk = _file_title_key(sp)
                    dp = os.path.join(canon_dir, fn)
                    if os.path.exists(dp) or (tk and tk in dtitles):
                        adst = os.path.join("/Volumes/MediaVolume3/Downloads/music/_attic_dupes/cover_identity",
                                            os.path.relpath(sp, "/Volumes/MediaVolume3/plexify-music"))
                        os.makedirs(os.path.dirname(adst), exist_ok=True)
                        _sh.move(sp, adst)
                        _log_recovery_move("cover_dupe", sp, adst)
                    else:
                        _sh.move(sp, dp)
                        _log_recovery_move("cover_merge", sp, dp)
                        if tk:
                            dtitles.add(tk)
                    moved += 1
                try:
                    if not os.listdir(d):
                        os.rmdir(d)
                        pd = os.path.dirname(d)
                        if os.path.isdir(pd) and not os.listdir(pd):
                            os.rmdir(pd)
                except OSError:
                    pass
                for x in rs:
                    x.status = "superseded"
                    x.note = ("same cover identity as %s — combined" % canon_dir)[:1024]
            if moved:
                out["merged"] += 1
                merged_dirs.append(canon_dir)
                # canonical row absorbs the folder's real file list
                try:
                    files = [os.path.join(canon_dir, f) for f in os.listdir(canon_dir)
                             if f.lower().endswith((".flac", ".mp3", ".m4a", ".alac", ".ogg", ".opus"))]
                    canon_rows[0].imported_paths = json.dumps(sorted(files))
                except OSError:
                    pass
                log.warning("cover_identity: combined %d folders into %s (same Spotify album %s)",
                            len(live), canon_dir, fa)
        s_.commit()
    if merged_dirs:
        try:
            from . import plex_client
            srv = plex_client._connect()
            sec = plex_client._music_section(srv) if srv else None
            if sec:
                for d in set(os.path.dirname(x) for x in merged_dirs):
                    sec.update(path=d.replace(_LOCAL_PATH_PREFIX, _plex_prefix(), 1))
        except Exception:
            log.exception("cover_identity: rescan failed")
    if out["ids_assigned"] or out["covers_assigned"] or out["merged"]:
        log.info("cover_identity_tick: %s", out)
    return out



# ===== Library hygiene: manifestation-level control (no file moves) =====

_INTENT_KEYS_CACHE: tuple = (0.0, frozenset())
_INTENT_ARTISTS_CACHE: tuple = (0.0, {})

def _intent_title_keys() -> frozenset:
    """Title keys of every song the user actually asked for (liked songs +
    playlist mirror). An album is ON PURPOSE iff an active row points at it or
    it contains one of these. (30-min cache.)"""
    global _INTENT_KEYS_CACHE
    ts, vals = _INTENT_KEYS_CACHE
    if time.time() - ts < 1800 and vals:
        return vals
    keys = set()
    try:
        from .db import SessionLocal, SpotifyLikedTrack, LocalTrack
        with SessionLocal() as s_:
            for (t,) in s_.query(SpotifyLikedTrack.title).all():
                if t:
                    keys.add(_norm_title_key(t))
            for (t,) in s_.query(LocalTrack.title).all():
                if t:
                    keys.add(_norm_title_key(t))
    except Exception:
        log.exception("_intent_title_keys failed")
    keys.discard("")
    if keys:
        _INTENT_KEYS_CACHE = (time.time(), frozenset(keys))
    return _INTENT_KEYS_CACHE[1]


_ARTIST_STOP = {"the", "and", "feat", "with", "band", "various", "artists", "music"}


def _artist_tokens(name: str) -> set:
    """Significant lowercase tokens of an artist name (len>=3, stopwords dropped).
    Used to tell a wrong-artist VERSION of a liked song from a legit collab credit:
    'Coolio & L.V.' shares {coolio} with liked 'Coolio'; 'Steve Winwood' shares
    nothing with liked 'Traffic'."""
    import re as _re
    return {p for p in _re.split(r"[^a-z0-9]+", (name or "").lower())
            if len(p) >= 3 and p not in _ARTIST_STOP}


def _intent_title_artists() -> dict:
    """title_key -> set of artist tokens, from liked songs + the playlist mirror.
    Lets the orphan sweep require a wanted title to ALSO come from a matching artist
    so a wrong-artist version can't pass the intent gate and hijack the liked song's
    Plex title-match. (30-min cache.)"""
    global _INTENT_ARTISTS_CACHE
    ts, vals = _INTENT_ARTISTS_CACHE
    if time.time() - ts < 1800 and vals:
        return vals
    m = {}
    try:
        from .db import SessionLocal, SpotifyLikedTrack, LocalTrack
        with SessionLocal() as s_:
            for (t, a) in s_.query(SpotifyLikedTrack.title, SpotifyLikedTrack.artist).all():
                if t:
                    m.setdefault(_norm_title_key(t), set()).update(_artist_tokens(a))
            for (t, a) in s_.query(LocalTrack.title, LocalTrack.artist).all():
                if t:
                    m.setdefault(_norm_title_key(t), set()).update(_artist_tokens(a))
    except Exception:
        log.exception("_intent_title_artists failed")
    m.pop("", None)
    if m:
        _INTENT_ARTISTS_CACHE = (time.time(), m)
    return m


def _corrupt_file_set() -> set:
    try:
        return set(json.load(open("/data/corrupt_files.json")))
    except Exception:
        return set()


def _album_names_match(a: str, b: str) -> bool:
    """Prefix-tolerant album-name comparison: 'Hot Space (Deluxe Remastered
    Version)' matches 'Hot Space'; 'Hallo Spaceboy' does not. Guards identity/
    cover assignment — the cover must belong to the album the row NAMES."""
    na, nb = _norm_title_key(a), _norm_title_key(b)
    return bool(na and nb and (na == nb or na.startswith(nb) or nb.startswith(na)))



def _assign_placeholder_cover(folder: str) -> bool:
    """Temporary FAKE cover: keeps a purposeful-but-unresolved album visible
    (it passes the cover criterion) until the identity system delivers the real
    art. A .plexify_placeholder marker flags it for replacement."""
    import shutil as _sh
    src = "/data/cover_placeholder.jpg"
    dest = os.path.join(folder, "cover.jpg")
    try:
        if not os.path.isfile(src) or os.path.exists(dest):
            return False
        _sh.copyfile(src, dest)
        os.chmod(dest, 0o664)
        with open(os.path.join(folder, ".plexify_placeholder"), "w") as fh:
            fh.write("temporary cover — replaced automatically when the real album art is assigned\n")
        return True
    except Exception:
        return False



def library_hygiene_tick(verify_batch: int = 60) -> dict:
    """Keep every album in Plex ON PURPOSE — without moving a single file.

    1. Rotating integrity spot-check (cursor-paged): corrupt files are added to
       /data/corrupt_files.json + hidden from Plex via .plexignore; a locked
       album that contains one is unlocked so completion re-downloads the song.
    2. Classify every album folder: purposeful (active row, or contains a
       liked/playlist song) vs accidental/broken. Unwanted ones are hidden by
       writing their paths into /Volumes/MediaVolume3/plexify-music/.plexignore — Plex drops them at the
       next scan; the files stay exactly where they are. Their rows stop
       rendering tiles. Fully reversible: remove the line, rescan.
    """
    from .spotify_catalog import is_blocked_comp_name as _sc_is_blocked_comp
    out = {"verified": 0, "new_corrupt": 0, "hidden_albums": 0, "unhidden": 0}
    if (get_config("acq_recovery_enabled", "0") or "0") != "1":
        out["skipped"] = "disabled"
        return out
    try:
        if _plex_active_video_sessions() > 0:
            out["skipped"] = "streaming"
            return out
    except Exception:
        pass
    from .db import SessionLocal, AutofillAction
    AUD = (".flac", ".mp3", ".m4a", ".alac", ".ogg", ".opus")
    corrupt = _corrupt_file_set()

    # ── 1. rotating integrity spot-check ──
    allfiles = []
    for artist in sorted(os.listdir("/Volumes/MediaVolume3/plexify-music")):
        ad = os.path.join("/Volumes/MediaVolume3/plexify-music", artist)
        if not os.path.isdir(ad) or artist.startswith(("_", ".")):
            continue
        for root, _d, fs in os.walk(ad):
            for f in fs:
                if f.lower().endswith(".flac"):
                    allfiles.append(os.path.join(root, f))
    allfiles.sort()
    try:
        cur = int(get_config("hygiene_verify_cursor", "0") or "0")
    except Exception:
        cur = 0
    batch = allfiles[cur:cur + verify_batch]
    set_config("hygiene_verify_cursor", str(0 if cur + verify_batch >= len(allfiles) else cur + verify_batch))
    if batch:
        _ok, _bad = _verify_flac_integrity(batch)
        out["verified"] = len(batch)
        for bp, _err in _bad:
            if bp not in corrupt:
                corrupt.add(bp)
                out["new_corrupt"] += 1
        if out["new_corrupt"]:
            json.dump(sorted(corrupt), open("/data/corrupt_files.json", "w"))
            with SessionLocal() as s_:
                for r in s_.query(AutofillAction).filter(
                        AutofillAction.status == "complete_locked").all():
                    try:
                        paths = json.loads(r.imported_paths or "[]")
                    except Exception:
                        continue
                    if any(p in corrupt for p in paths):
                        r.status = "imported"
                        r.note = "unlocked: corrupt file detected, needs refill"[:1024]
                s_.commit()

    # ── 2. classify albums -> hide in PLEX (collection with hide-items mode).
    # The albums stay fully indexed in Plex's database — the scanner considers
    # them already cataloged so they can never re-form — they just stop
    # appearing in library views. No files are touched.
    # HIDE criteria: broken (no good audio) · accidental (no row, no intent
    # match) · NO PURPOSEFUL COVER (everything on purpose has a designated
    # cover from its Spotify origin; a coverless album hasn't proven intent —
    # it un-hides automatically the moment the cover-identity system assigns one).
    intent = _intent_title_keys()
    # READINESS GATE for the cover criterion: while the identity backfill is
    # still resolving rows, "no cover yet" mostly means "not processed yet" —
    # only enforce cover-based hiding once the backlog is nearly drained, so
    # legitimate albums are never mass-hidden during catch-up.
    with SessionLocal() as s_:
        _unresolved = (s_.query(AutofillAction)
                       .filter(AutofillAction.status.in_(("imported", "complete_locked")),
                               (AutofillAction.cover_url.is_(None)) | (AutofillAction.cover_url == ""))
                       .count())
    # User decision (2026-06-12): the cover rule applies IMMEDIATELY — hidden
    # albums un-hide automatically as the identity system assigns their covers.
    out["identity_unresolved"] = _unresolved
    row_dirs = {}
    covered_dirs = set()
    with SessionLocal() as s_:
        for r in s_.query(AutofillAction).filter(
                AutofillAction.status.in_(("imported", "complete_locked")),
                AutofillAction.imported_paths.isnot(None)).all():
            try:
                paths = json.loads(r.imported_paths or "[]")
            except Exception:
                continue
            if not paths:
                continue
            d = os.path.dirname(paths[0])
            row_dirs[d] = r
            if (r.cover_url or "").strip():
                covered_dirs.add(d)
    unwanted = {}
    for artist in sorted(os.listdir("/Volumes/MediaVolume3/plexify-music")):
        ad = os.path.join("/Volumes/MediaVolume3/plexify-music", artist)
        if not os.path.isdir(ad) or artist.startswith(("_", ".")):
            continue
        for album in sorted(os.listdir(ad)):
            d = os.path.join(ad, album)
            if not os.path.isdir(d):
                continue
            # Part 2 (2026-06-15): generic hits comps ("Now That's What I Call Music!")
            # are never wanted — hide the folder even when it happens to hold liked songs.
            if _sc_is_blocked_comp(album):
                unwanted[d] = "blocked compilation (Now That's What I Call\u2026)"
                continue
            files = [f for f in os.listdir(d) if f.lower().endswith(AUD)]
            good = [f for f in files if os.path.join(d, f) not in corrupt]
            if not good:
                unwanted[d] = "broken"
                continue
            has_cover = (d in covered_dirs) or os.path.isfile(os.path.join(d, "cover.jpg"))
            if d in row_dirs:
                if not has_cover and _assign_placeholder_cover(d):
                    out["placeholders"] = out.get("placeholders", 0) + 1
                continue
            hit = False
            for f in good[:20]:
                if _file_title_key(os.path.join(d, f), artist) in intent:
                    hit = True
                    break
            if not hit:
                unwanted[d] = "accidental"
            elif not has_cover and _assign_placeholder_cover(d):
                out["placeholders"] = out.get("placeholders", 0) + 1

    # Part 1 (2026-06-15): hide an INCOMPLETE album until it actually contains the liked
    # song it was acquired for; it un-hides automatically once the song lands (recomputed
    # every pass). Reversible — collection membership only, no files touched.
    try:
        from .db import SpotifyLikedTrack as _SLT
        with SessionLocal() as _ls:
            _lk_title = {lt.spotify_track_id: (lt.title or "") for lt in _ls.query(_SLT).all()}
            for r in _ls.query(AutofillAction).filter(
                    AutofillAction.status == "imported",
                    AutofillAction.imported_paths.isnot(None)).all():
                try:
                    _ps = json.loads(r.imported_paths or "[]")
                except Exception:
                    continue
                if not _ps:
                    continue
                _d = os.path.dirname(_ps[0])
                for _old in ("/plexify-music/",):
                    if _d.startswith(_old):
                        _d = "/Volumes/MediaVolume3/plexify-music/" + _d[len(_old):]; break
                if (not os.path.isdir(_d)) or (_d in unwanted):
                    continue
                try:
                    _tids = json.loads(r.track_ids_json or "[]")
                except Exception:
                    _tids = []
                _need = {_norm_title_key(_lk_title[t]) for t in _tids if t in _lk_title and _lk_title[t]}
                _need.discard("")
                if not _need:
                    continue  # row covers no liked songs -> not subject to this rule
                try:
                    _fs = [f for f in os.listdir(_d) if f.lower().endswith(AUD)]
                except Exception:
                    _fs = []
                _present = set()
                for f in _fs[:40]:
                    k = _file_title_key(os.path.join(_d, f), os.path.basename(os.path.dirname(_d)))
                    if k:
                        _present.add(k)
                _has = any(k == w or w in k or k in w for w in _need for k in _present)
                if not _has:
                    unwanted[_d] = "awaiting its liked song"
    except Exception:
        log.exception("hygiene: awaiting-liked classification failed")

    # map folders -> Plex albums via one full track listing
    try:
        from . import plex_client
        srv = plex_client._connect()
        sec = plex_client._music_section(srv) if srv else None
    except Exception:
        srv = sec = None
    if sec:
        folder_to_rk = {}
        folder_meta = {}
        try:
            for t in sec.searchTracks():
                loc = (t.locations or [None])[0]
                if loc:
                    d = os.path.dirname(loc.replace(_plex_prefix(), _LOCAL_PATH_PREFIX, 1))
                    folder_to_rk[d] = t.parentRatingKey
                    folder_meta[d] = {"rk": t.parentRatingKey,
                                      "album": t.parentTitle or "",
                                      "artist": t.grandparentTitle or ""}
        except Exception:
            log.exception("hygiene: track listing failed")
        # THE MAP: folder <-> Plex album, the join key every mechanism can use
        # (Albums-page deep links, drift repair, path-based matching).
        try:
            json.dump(folder_meta, open("/data/folder_album_map.json", "w"))
        except Exception:
            pass
        # LIBRARY-WIDE ARTIST DRIFT: folders whose parent dir disagrees with the
        # Plex artist (the chimera-leftover class, e.g. Kygo singles filed under
        # The Velvet Underground). The verify tick dances these — this extends
        # its recently-added window to the whole library.
        try:
            drift = []
            for d, m in folder_meta.items():
                fa = os.path.basename(os.path.dirname(d))
                pa = m["artist"]
                ka, kp = _norm_title_key(fa), _norm_title_key(pa)
                if not ka or not kp:
                    continue
                if ka == kp or ka.startswith(kp) or kp.startswith(ka):
                    continue
                if kp == _norm_title_key("Various Artists") or ka == _norm_title_key("Various Artists"):
                    continue
                drift.append({"folder": d, "plex_artist": pa, "dir_artist": fa, "rk": m["rk"]})
            json.dump(drift, open("/data/artist_drift.json", "w"))
            if drift:
                out["artist_drift"] = len(drift)
        except Exception:
            log.exception("hygiene: drift scan failed")
        want_rks = {folder_to_rk[d] for d in unwanted if d in folder_to_rk}
        COLL = "Plexify · Hidden"
        try:
            coll = None
            for c in sec.collections():
                if c.title == COLL:
                    coll = c
                    break
            current = set()
            if coll:
                current = {a.ratingKey for a in coll.items()}
            to_add = [rk for rk in want_rks - current]
            to_del = [rk for rk in current - want_rks]
            if to_add:
                items = [srv.fetchItem(rk) for rk in to_add[:1000]]
                if coll is None:
                    coll = sec.createCollection(COLL, items=items)
                else:
                    coll.addItems(items)
                out["hidden_albums"] = len(items)
            if coll and to_del:
                coll.removeItems([srv.fetchItem(rk) for rk in to_del[:1000]])
                out["unhidden"] = len(to_del)
            if coll:
                try:
                    coll.modeUpdate(mode="hideItems")
                except Exception:
                    pass
        except Exception:
            log.exception("hygiene: collection update failed")
    # RULE 2 absorb-or-hide verdicts join the hidden set
    try:
        for d in json.load(open("/data/rules_hidden.json")):
            if os.path.isdir(d) and d not in unwanted:
                unwanted[d] = "rule 2: near-duplicate of a sibling album"
    except Exception:
        pass
    # the app's Albums page reads this to drop hidden tiles
    json.dump(sorted(unwanted), open("/data/hidden_albums.json", "w"))
    json.dump({k: v for k, v in unwanted.items()}, open("/data/hidden_albums_reasons.json", "w"))
    if out["hidden_albums"] or out["unhidden"] or out["new_corrupt"]:
        log.warning("library_hygiene: %d newly hidden, %d restored, %d new corrupt (total hidden: %d)",
                    out["hidden_albums"], out["unhidden"], out["new_corrupt"], len(unwanted))
    return out



def album_rules_tick(merge_batch: int = 8, dedupe_batch: int = 40, rehome_batch: int = 1) -> dict:
    """The album rulebook, applied continuously:

    RULE 2 (same artist only): albums whose names match after stripping
    anything in parenthesis, or which share an uncommon word/term (1989 — a
    term that isn't usual English; never 'the'), are combined. Pairs that fail
    both name tests but are mostly the same songs may not coexist: one absorbs
    the other or the smaller goes to the hidden collection.
    RULE 3: one version of a song per album — exact-version duplicates keep the
    best copy (largest file); remixes/remasters/live variants stay distinct.
    RULE 4: an album is never created from a counterpart of more than 50
    songs. Existing engine-built monsters are re-homed: each liked song moves
    (retagged) into its best <=50 counterpart album, non-liked filler goes to
    the attic, and the monster row retires. Pre-existing library albums
    (library_existing / no row) are never touched.
    (RULE 1 — same cover image combines — lives in cover_identity_tick.)
    """
    import shutil as _sh, re as _re
    out = {"r2_merged": 0, "r2_hidden": 0, "r3_atticed": 0, "r4_rehomed": 0, "r4_filler": 0}
    if (get_config("acq_recovery_enabled", "0") or "0") != "1":
        out["skipped"] = "disabled"
        return out
    try:
        if _plex_active_video_sessions() > 0:
            out["skipped"] = "streaming"
            return out
    except Exception:
        pass
    AUD = (".flac", ".mp3", ".m4a", ".alac", ".ogg", ".opus")
    EDITION = _re.compile(
        r"\b(deluxe|edition|remaster(ed)?|expanded|anniversary|special|bonus|"
        r"complete|collector'?s|super|ultimate|version|mono|stereo|taylor'?s)\b",
        _re.IGNORECASE)
    COMMON = set("the a an of and or in on at to for from with without is are was be "
                 "i you he she it we they my your his her its our their this that these "
                 "best greatest hits live album songs music original motion picture "
                 "soundtrack vol volume part pt one two three new old big little love "
                 "you me all day night time life world heart go gone going "
                 "collection anthology presents featuring feat single ep lp disc disk "
                 "across series session sessions musical holiday christmas winter "
                 "summer featured inspired".split())

    def _name_core(n):
        # RULE 2a identity: the name minus ANYTHING in parenthesis/brackets.
        n = _re.sub(r"\(.*?\)|\[.*?\]", " ", n or "")
        return _norm_title_key(n)

    def _uncommon(n):
        toks = set()
        base = _re.sub(r"\(.*?\)|\[.*?\]", " ", (n or "").lower())
        for w in _re.findall(r"[a-z0-9']+", base):
            if w in COMMON or EDITION.fullmatch(w):
                continue
            if w.isdigit() and len(w) >= 3:
                toks.add(w)
            elif len(w) >= 5:
                toks.add(w)
        return toks

    def _audio(d):
        try:
            return [f for f in os.listdir(d) if f.lower().endswith(AUD)]
        except OSError:
            return []

    hidden_extra = []
    try:
        hidden_extra = json.load(open("/data/rules_hidden.json"))
    except Exception:
        pass
    hidden_set = set(hidden_extra)

    # ── RULE 2 (same artist only) ──
    # 2a: same name minus ANYTHING in parenthesis -> combined.
    # 2b: different core names but a shared UNCOMMON word/term (1989,
    #     hamilton — words that aren't usual English) -> combined.
    # Failing both while being mostly the same songs -> absorb or hide one.
    merged = 0

    def _combine(artist, ad, src_album, dst_album):
        """Move src folder's songs into dst; same-title copies go to the attic."""
        src, canon = os.path.join(ad, src_album), os.path.join(ad, dst_album)
        if not os.path.isdir(src) or not os.path.isdir(canon) or src == canon:
            return False
        titles = _folder_title_keys(canon)
        for fn in _audio(src):
            sp = os.path.join(src, fn)
            tk = _file_title_key(sp, artist)
            dp = os.path.join(canon, fn)
            if os.path.exists(dp) or (tk and tk in titles):
                adst = os.path.join("/Volumes/MediaVolume3/Downloads/music/_attic_dupes/rule2", artist, src_album, fn)
                os.makedirs(os.path.dirname(adst), exist_ok=True)
                _sh.move(sp, adst); _log_recovery_move("rule2_dupe", sp, adst)
            else:
                _sh.move(sp, dp); _log_recovery_move("rule2_merge", sp, dp)
                if tk:
                    titles.add(tk)
        try:
            if not os.listdir(src):
                os.rmdir(src)
        except OSError:
            pass
        log.warning("rule2: combined %r/%r into %r", artist, src_album, dst_album)
        return True

    # Rotating cursor: never restart at 'A' every run — late-alphabet artists
    # must get their merges too (the Florence/Verve lesson).
    _arts = [a for a in sorted(os.listdir("/Volumes/MediaVolume3/plexify-music"))
             if os.path.isdir(os.path.join("/Volumes/MediaVolume3/plexify-music", a)) and not a.startswith(("_", "."))]
    try:
        _c2 = int(get_config("rule2_cursor", "0") or "0")
    except Exception:
        _c2 = 0
    if _c2 >= len(_arts):
        _c2 = 0
    _p2 = _c2
    for artist in _arts[_c2:_c2 + 120]:
        if merged >= merge_batch:
            break
        _p2 += 1
        ad = os.path.join("/Volumes/MediaVolume3/plexify-music", artist)
        albums = [a for a in os.listdir(ad) if os.path.isdir(os.path.join(ad, a))]
        if len(albums) < 2:
            continue
        # 2a: same name minus anything in parenthesis
        cores = {}
        for a in albums:
            cores.setdefault(_name_core(a) or a.lower(), []).append(a)
        for core, group in cores.items():
            if len(group) < 2 or merged >= merge_batch:
                continue
            group.sort(key=lambda a: -len(_audio(os.path.join(ad, a))))
            for a in group[1:]:
                if os.path.join(ad, a) in hidden_set:
                    continue
                if _combine(artist, ad, a, group[0]):
                    merged += 1
                    out["r2_merged"] += 1
        if merged >= merge_batch:
            break
        # 2b + absorb-or-hide, pairwise over what's left.
        # NEVER under Various Artists: a catch-all pseudo-artist where shared
        # words mean shared FRANCHISE, not same work (the Shrek 2 lesson).
        if _normalize_for_key(artist) in ("variousartists", "various", "va",
                                          "soundtrack", "unknownartist", "originalcast"):
            # ...EXCEPT: VA soundtracks/cast recordings for the SAME film or show
            # DO combine. The film/show title is extracted and validated against
            # the local movie+TV title DB (title_db, from TMDB exports), so only a
            # real, identical title merges — "Dark Knight" never joins "Dark Knight
            # Rises", "Psych: The Musical" never joins "Shrek The Musical".
            try:
                from .title_db import soundtrack_key as _stk
                st = {}
                for a in [x for x in os.listdir(ad) if os.path.isdir(os.path.join(ad, x))]:
                    k = _stk(a)
                    if k:
                        st.setdefault(k, []).append(a)
                for k, grp in st.items():
                    if len(grp) < 2 or merged >= merge_batch:
                        continue
                    grp.sort(key=lambda a: -len(_audio(os.path.join(ad, a))))
                    for a in grp[1:]:
                        if os.path.join(ad, a) in hidden_set:
                            continue
                        if _combine(artist, ad, a, grp[0]):
                            merged += 1
                            out["r2_merged"] += 1
                            log.warning("rule2-VA-soundtrack: %r -> %r (title=%r)", a, grp[0], k)
            except Exception as e:
                log.warning("VA soundtrack merge skipped for %r: %s", artist, e)
            continue
        alive = sorted(a for a in os.listdir(ad) if os.path.isdir(os.path.join(ad, a)))
        for i_ in range(len(alive)):
            for j_ in range(i_ + 1, len(alive)):
                if merged >= merge_batch:
                    break
                ai, aj = alive[i_], alive[j_]
                p1, p2 = os.path.join(ad, ai), os.path.join(ad, aj)
                if p1 in hidden_set or p2 in hidden_set:
                    continue
                if not os.path.isdir(p1) or not os.path.isdir(p2):
                    continue
                if _name_core(ai) == _name_core(aj):
                    continue  # 2a's job
                if _uncommon(ai) & _uncommon(aj):
                    # 2b: same uncommon term -> same work; bigger absorbs smaller
                    dst, src = (ai, aj) if len(_audio(p1)) >= len(_audio(p2)) else (aj, ai)
                    if _combine(artist, ad, src, dst):
                        merged += 1
                        out["r2_merged"] += 1
                    continue
                # neither name test passes: if they're mostly the same songs they
                # may not coexist — absorb-or-hide (the smaller one hides).
                t1, t2 = _folder_title_keys(p1, artist), _folder_title_keys(p2, artist)
                if not t1 or not t2:
                    continue
                inter = len(t1 & t2)
                if inter >= 3 and inter >= 0.6 * min(len(t1), len(t2)):
                    loser = p1 if len(t1) < len(t2) else p2
                    hidden_set.add(loser)
                    out["r2_hidden"] += 1
                    log.warning("rule2: %r mostly duplicates its sibling — hiding it", loser)
    set_config("rule2_cursor", str(_p2 if _p2 < len(_arts) else 0))
    json.dump(sorted(hidden_set), open("/data/rules_hidden.json", "w"))

    # ── RULE 3: one version per song per album ──
    # Rotating cursor: title keys mean tag reads (expensive on the NAS), so
    # each run dedupes a window of folders and resumes where the last stopped.
    atticed = 0
    folders = []
    for artist in sorted(os.listdir("/Volumes/MediaVolume3/plexify-music")):
        ad = os.path.join("/Volumes/MediaVolume3/plexify-music", artist)
        if not os.path.isdir(ad) or artist.startswith(("_", ".")):
            continue
        for album in sorted(os.listdir(ad)):
            if os.path.isdir(os.path.join(ad, album)):
                folders.append((artist, album))
    try:
        _cur = int(get_config("rule3_cursor", "0") or "0")
    except Exception:
        _cur = 0
    if _cur >= len(folders):
        _cur = 0
    _pos = _cur
    for artist, album in folders[_cur:_cur + 150]:
        if atticed >= dedupe_batch:
            break
        _pos += 1
        ad = os.path.join("/Volumes/MediaVolume3/plexify-music", artist)
        d = os.path.join(ad, album)
        by_key = {}
        for fn in _audio(d):
            k = _file_title_key(os.path.join(d, fn), artist)
            if k:
                by_key.setdefault(k, []).append(fn)
        for k, fns in by_key.items():
            if len(fns) < 2:
                continue
            fns.sort(key=lambda f: -os.path.getsize(os.path.join(d, f)))
            for fn in fns[1:]:   # keep the best (largest) copy
                sp = os.path.join(d, fn)
                adst = os.path.join("/Volumes/MediaVolume3/Downloads/music/_attic_dupes/rule3", artist, album, fn)
                os.makedirs(os.path.dirname(adst), exist_ok=True)
                _sh.move(sp, adst); _log_recovery_move("rule3_version", sp, adst)
                atticed += 1
                out["r3_atticed"] += 1
    set_config("rule3_cursor", str(_pos if _pos < len(folders) else 0))

    # ── RULE 4: no album may be built from a counterpart of >50 songs ──
    # Existing engine-built monsters are dismantled: liked songs re-home into
    # their best <=50 counterpart albums (best_album_for_liked is itself
    # rule-4 gated now), filler tracks fetched only to complete the monster go
    # to the attic, and the monster row retires. Only imported/complete_locked
    # rows qualify — the user's own pre-existing library is never touched.
    from .spotify_catalog import best_album_for_liked as _bafl
    from .db import SessionLocal as _SL, AutofillAction as _AA, SpotifyAlbum as _SA2, SpotifyLikedTrack as _SLT
    rehomed = 0
    with _SL() as s_:
        by_id, by_name, img_by_id, by_type = {}, {}, {}, {}
        for a in s_.query(_SA2).all():
            by_type[a.album_id] = getattr(a, "album_type", None)
            t = int(a.total_tracks or 0)
            if t <= 0:
                continue
            by_id[a.album_id] = t
            by_name[(_normalize_for_key(a.album_artist or ""), _normalize_for_key(a.name or ""))] = t
            img_by_id[a.album_id] = getattr(a, "image_url", None) or ""
        liked_by_key = {}
        for lt in s_.query(_SLT).all():
            liked_by_key[(_normalize_for_key(lt.artist or ""), _norm_title_key(lt.title or ""))] = lt.spotify_track_id
        intent = _intent_title_keys()  # liked + playlist titles — never attic these
        for r in s_.query(_AA).filter(_AA.status.in_(("imported", "complete_locked"))).all():
            if rehomed >= rehome_batch:
                break
            if (r.note or "").startswith("rule 4"):
                continue  # already dismantled — the creation gates stop regrowth
            fa = (r.foreign_album_id or "")
            total = by_id.get(fa[3:]) if fa.startswith("sp:") else None
            if total is None:
                total = by_name.get((_normalize_for_key(r.artist or ""), _normalize_for_key(r.album or "")))
            d = os.path.join("/Volumes/MediaVolume3/plexify-music", _safe_for_fs(r.artist or ""), _safe_for_fs(r.album or ""))
            try:
                from .spotify_catalog import is_fake_album_name as _ifa2, is_cover_product_name as _icp
                _fa_id = fa[3:] if fa.startswith("sp:") else None
                fake = _ifa2(r.album or "", by_type.get(_fa_id))
                cover_prod = _icp(r.album or "")
            except Exception:
                fake = cover_prod = False
            monster = bool(total and int(total) > 50)
            if not monster and total is None and os.path.isdir(d) and len(_audio(d)) > 50:
                # Unknown counterpart: judge by DISTINCT songs, not raw files —
                # version dupes (rule 3's job) must never fake a monster
                # (the Dusk at Cubist Castle lesson: 27 songs, 54 files).
                keys = {_file_title_key(os.path.join(d, f), r.artist or "") or f
                        for f in _audio(d)}
                monster = len(keys) > 50
            # A fake/derivative album (hits/collection/lullaby/tribute) is
            # dismantled the same way: liked songs re-home into real albums,
            # everything else attics. _bad describes which gate fired.
            _bad = "fake" if fake else ("monster" if monster else "")
            if not _bad:
                continue
            if not os.path.isdir(d):
                r.status = "superseded"
                r.note = ("rule 4: counterpart has %s songs — retired, never built" % total)[:1024]
                rehomed += 1
                out["r4_rehomed"] += 1
                continue
            akey = _normalize_for_key(r.artist or "")
            moved, kept, fillered = 0, 0, 0
            dests = {}
            for fn in sorted(_audio(d)):
                sp = os.path.join(d, fn)
                tk = _file_title_key(sp, r.artist or "")
                tid = liked_by_key.get((akey, _norm_title_key(tk))) if tk else None
                dest = _bafl(tid) if tid else None
                if dest:
                    nm_ = dest["name"]
                    vd = os.path.join("/Volumes/MediaVolume3/plexify-music", _safe_for_fs(r.artist or ""), _safe_for_fs(nm_))
                    os.makedirs(vd, exist_ok=True)
                    dp = os.path.join(vd, fn)
                    if os.path.exists(dp):
                        adst = os.path.join("/Volumes/MediaVolume3/Downloads/music/_attic_dupes/rule4",
                                            _safe_for_fs(r.artist or ""), _safe_for_fs(r.album or ""), fn)
                        os.makedirs(os.path.dirname(adst), exist_ok=True)
                        _sh.move(sp, adst); _log_recovery_move("rule4_dupe", sp, adst)
                        fillered += 1
                    else:
                        _sh.move(sp, dp); _log_recovery_move("rule4_rehome", sp, dp)
                        try:
                            from mutagen import File as _MF
                            m = _MF(dp, easy=True)
                            if m is not None:
                                m["album"] = nm_
                                m.save()
                        except Exception:
                            pass
                        dests.setdefault((dest["album_id"], nm_), []).append(dp)
                        moved += 1
                elif (tid or (tk and tk in intent)) and not cover_prod:
                    kept += 1  # asked-for song with no <=50 home found — stays put
                else:
                    adst = os.path.join("/Volumes/MediaVolume3/Downloads/music/_attic_dupes/rule4",
                                        _safe_for_fs(r.artist or ""), _safe_for_fs(r.album or ""), fn)
                    os.makedirs(os.path.dirname(adst), exist_ok=True)
                    _sh.move(sp, adst); _log_recovery_move("rule4_filler", sp, adst)
                    fillered += 1
            for (aid_, nm_), paths in dests.items():
                ak2, alk2 = _normalize_for_key(r.artist or ""), _normalize_for_key(nm_)
                row2 = s_.query(_AA).filter_by(artist_key=ak2, album_key=alk2).first()
                if row2 is None:
                    row2 = _AA(artist=r.artist, album=nm_, artist_key=ak2, album_key=alk2,
                               status="imported", foreign_album_id="sp:" + aid_,
                               note=("rule 4: re-homed from %r" % (r.album or ""))[:1024],
                               last_attempt_at=datetime.utcnow(), attempt_count=0)
                    s_.add(row2)
                if not (row2.foreign_album_id or "").startswith("sp:"):
                    row2.foreign_album_id = "sp:" + aid_
                if not (row2.cover_url or "").strip() and img_by_id.get(aid_):
                    row2.cover_url = img_by_id[aid_]
                try:
                    prev = json.loads(row2.imported_paths or "[]")
                except Exception:
                    prev = []
                row2.imported_paths = json.dumps(sorted(set(prev) | set(paths)))
                if row2.status not in ("imported", "complete_locked"):
                    row2.status = "imported"
            if not _audio(d):
                # only non-audio remnants (cover.jpg etc) left — attic them, drop the dir
                for x in os.listdir(d):
                    adst = os.path.join("/Volumes/MediaVolume3/Downloads/music/_attic_dupes/rule4",
                                        _safe_for_fs(r.artist or ""), _safe_for_fs(r.album or ""), x)
                    os.makedirs(os.path.dirname(adst), exist_ok=True)
                    _sh.move(os.path.join(d, x), adst)
                try:
                    os.rmdir(d)
                except OSError:
                    pass
                r.status = "superseded"
                r.note = ("rule 4: %s-song counterpart dismantled — %d re-homed into %d albums, %d filler atticed"
                          % (total, moved, len(dests), fillered))[:1024]
            else:
                if r.status == "complete_locked":
                    r.status = "imported"  # a monster is never 'complete'
                r.note = ("rule 4: monster counterpart (%s songs) — %d re-homed, %d filler atticed, %d kept (no smaller home)"
                          % (total, moved, fillered, kept))[:1024]
            rehomed += 1
            out["r4_rehomed"] += 1
            out["r4_filler"] += fillered
            log.warning("rule4: dismantled %r/%r (counterpart %s songs): %d re-homed into %d albums, %d filler atticed, %d kept",
                        r.artist, r.album, total, moved, len(dests), fillered, kept)
        s_.commit()

    if any(out.get(k) for k in ("r2_merged", "r2_hidden", "r3_atticed", "r4_rehomed")):
        try:
            from . import plex_client
            srv = plex_client._connect()
            sec = plex_client._music_section(srv) if srv else None
            if sec:
                sec.update()
        except Exception:
            pass
        log.info("album_rules_tick: %s", out)
    return out



def plex_tile_reconcile_tick(window: int = 120, max_merges: int = 30) -> dict:
    """Merge co-located Plex album tiles to disk truth. After file-level merges
    (rule 2 / VA soundtrack), multiple album TAGS share one folder and Plex shows
    separate tiles. This walks a rotating window of ARTISTS (co-located tiles are
    always same-artist, so they group correctly here), groups each artist's albums
    by track folder, and merges every folder's tiles into the largest — setting the
    poster from a real cover.jpg. Catches the different-title same-folder case that
    Phase 3b (same-title) and Phase 3 (recently-added only) of the verify tick miss.
    Cursor-paged so cost stays bounded; full library covered over a few runs."""
    out = {"merged": 0}
    if (get_config("acq_recovery_enabled", "0") or "0") != "1":
        out["skipped"] = "disabled"
        return out
    try:
        if _plex_active_video_sessions() > 0:
            out["skipped"] = "streaming"
            return out
    except Exception:
        pass
    try:
        from . import plex_client
        srv = plex_client._connect()
        sec = plex_client._music_section(srv) if srv else None
        if not sec:
            return out
        artists = sec.search(libtype="artist")
        n = len(artists)
        if not n:
            return out
        try:
            cur = int(get_config("tile_reconcile_cursor", "0") or "0")
        except Exception:
            cur = 0
        if cur >= n:
            cur = 0
        set_config("tile_reconcile_cursor", str(cur + window if cur + window < n else 0))
        for art in artists[cur:cur + window]:
            if out["merged"] >= max_merges:
                break
            try:
                albums = art.albums()
            except Exception:
                continue
            byfolder = {}
            for al in albums:
                try:
                    d = os.path.dirname(al.tracks()[0].media[0].parts[0].file)
                except Exception:
                    continue
                byfolder.setdefault(d, []).append(al)
            for d, tiles in byfolder.items():
                if len(tiles) < 2 or out["merged"] >= max_merges:
                    continue
                keep = max(tiles, key=lambda a: len(a.tracks()))
                ids = ",".join(str(a.ratingKey) for a in tiles if a.ratingKey != keep.ratingKey)
                try:
                    srv.query("/library/metadata/%s/merge?ids=%s" % (keep.ratingKey, ids),
                              method=srv._session.put)
                    out["merged"] += 1
                    cj = os.path.join(d, "cover.jpg")
                    if os.path.exists(cj) and not os.path.exists(os.path.join(d, ".plexify_placeholder")):
                        try:
                            keep.uploadPoster(filepath=cj)
                        except Exception:
                            pass
                    log.warning("plex_tile_reconcile: merged %d tiles -> %r (%s)",
                                len(tiles), keep.title, art.title)
                except Exception:
                    log.exception("plex_tile_reconcile: merge failed for %r", getattr(keep, "title", "?"))
    except Exception:
        log.exception("plex_tile_reconcile_tick failed")
    if out["merged"]:
        log.info("plex_tile_reconcile_tick: %s", out)
    return out


def shadow_album_cleanup_tick(batch: int = 3) -> dict:
    """Kill per-track-artist SHADOW copies of Various Artists albums.

    A liked soundtrack song queues under its TRACK artist before background
    MusicBrainz enrichment learns the album is really 'Various Artists' — so the
    library grows e.g. 'Ryan Gosling/Barbie The Album' next to the real
    'Various Artists/Barbie The Album'. Nothing ever re-homed them — until now.

    Disk-driven: any non-VA album folder whose album is CONFIRMED Various
    Artists (MusicBrainz cache, or a soundtrack-keyword title) gets merged into
    the VA folder: same-song dupes -> attic, unique tracks moved + retagged
    albumartist='Various Artists', empty shadow folder removed, both paths
    rescanned in Plex, matching rows superseded. Ledgered + reversible.
    """
    import shutil as _sh, re as _re
    out = {"merged_albums": 0, "moved": 0, "dupes": 0, "skipped_unconfirmed": 0}
    if (get_config("acq_recovery_enabled", "0") or "0") != "1":
        out["skipped"] = "disabled"
        return out
    try:
        if _plex_active_video_sessions() > 0:
            out["skipped"] = "streaming"
            return out
    except Exception:
        pass
    from .db import SessionLocal, MbAlbumMeta, AutofillAction
    _SOUND = _re.compile(r"soundtrack|motion picture|original cast|musical|broadway", _re.I)
    VA = "/Volumes/MediaVolume3/plexify-music/Various Artists"
    AUDIO = (".flac", ".mp3", ".m4a", ".alac", ".aac", ".ogg", ".opus", ".wav")

    def _confirmed_va(album_name: str) -> bool:
        if _SOUND.search(album_name or ""):
            return True
        bk = _normalize_for_key(album_name or "")
        if not bk:
            return False
        try:
            with SessionLocal() as s_:
                return bool(s_.query(MbAlbumMeta).filter(
                    MbAlbumMeta.album_key == bk,
                    MbAlbumMeta.status == "ok",
                    MbAlbumMeta.album_artist.ilike("various artists")).first())
        except Exception:
            return False

    merged_dirs = []
    try:
        for artist in sorted(os.listdir("/Volumes/MediaVolume3/plexify-music")):
            if out["merged_albums"] >= batch:
                break
            if artist.startswith(("_", ".")) or artist == "Various Artists":
                continue
            ad = os.path.join("/Volumes/MediaVolume3/plexify-music", artist)
            if not os.path.isdir(ad):
                continue
            for album in os.listdir(ad):
                if out["merged_albums"] >= batch:
                    break
                src = os.path.join(ad, album)
                if not os.path.isdir(src):
                    continue
                if not _confirmed_va(album):
                    continue
                dest = os.path.join(VA, album)
                os.makedirs(dest, exist_ok=True)
                dtitles = _folder_title_keys(dest)
                moved_any = False
                for fn in list(os.listdir(src)):
                    sp = os.path.join(src, fn)
                    if not fn.lower().endswith(AUDIO):
                        continue
                    tk = _file_title_key(sp, artist)
                    dp = os.path.join(dest, fn)
                    if os.path.exists(dp) or (tk and tk in dtitles):
                        adst = os.path.join("/Volumes/MediaVolume3/Downloads/music/_attic_dupes/shadow_albums",
                                            artist, album, fn)
                        os.makedirs(os.path.dirname(adst), exist_ok=True)
                        _sh.move(sp, adst)
                        _log_recovery_move("shadow_dupe", sp, adst)
                        out["dupes"] += 1
                    else:
                        _sh.move(sp, dp)
                        try:
                            os.chmod(dp, 0o664)
                            from mutagen import File as _MF
                            m = _MF(dp, easy=True)
                            m["albumartist"] = "Various Artists"
                            m.save()
                        except Exception:
                            pass
                        _log_recovery_move("shadow_merge", sp, dp)
                        if tk:
                            dtitles.add(tk)
                        out["moved"] += 1
                    moved_any = True
                # tidy the emptied shadow folder (junk -> attic, never delete)
                try:
                    left = os.listdir(src)
                    if not left:
                        os.rmdir(src)
                        if not os.listdir(ad):
                            os.rmdir(ad)
                    elif moved_any:
                        adst = os.path.join("/Volumes/MediaVolume3/Downloads/music/_attic_dupes/shadow_albums",
                                            artist, album + "_leftovers")
                        os.makedirs(os.path.dirname(adst), exist_ok=True)
                        _sh.move(src, adst)
                        _log_recovery_move("shadow_leftovers", src, adst)
                except OSError:
                    pass
                if moved_any:
                    out["merged_albums"] += 1
                    merged_dirs.append((artist, album, dest))
                    # supersede the shadow row; revive/repoint the VA row
                    try:
                        ak = _normalize_for_key(artist)
                        bk = _normalize_for_key(album)
                        with session_scope() as s_:
                            for r in s_.query(AutofillAction).filter(
                                    AutofillAction.artist_key == ak,
                                    AutofillAction.album_key == bk).all():
                                r.status = "superseded"
                                r.note = ("shadow of Various Artists/%s — merged" % album)[:1024]
                            var = s_.query(AutofillAction).filter(
                                AutofillAction.artist_key == _normalize_for_key("Various Artists"),
                                AutofillAction.album_key == bk).first()
                            if var and var.status in ("pruned", "abandoned"):
                                var.status = "imported"
                                var.note = ("revived — shadow albums merged in")[:1024]
                    except Exception:
                        log.exception("shadow cleanup: row update failed for %s/%s", artist, album)
                    log.warning("shadow_album_cleanup: merged %r/%r into Various Artists (%d files, %d dupes atticed)",
                                artist, album, out["moved"], out["dupes"])
    except Exception:
        log.exception("shadow_album_cleanup_tick failed")
    # rescan the touched paths
    if merged_dirs:
        try:
            from . import plex_client
            srv = plex_client._connect()
            sec = plex_client._music_section(srv) if srv else None
            if sec:
                seen = set()
                for artist, album, dest in merged_dirs:
                    for d in (_plex_prefix() + "/" + artist, dest.replace(_LOCAL_PATH_PREFIX, _plex_prefix(), 1)):
                        if d not in seen:
                            sec.update(path=d)
                            seen.add(d)
        except Exception:
            log.exception("shadow cleanup: rescan failed")
        _report = {"action": "shadow_cleanup", "merged": [(a, b) for a, b, _ in merged_dirs]}
        try:
            _report["ts"] = time.time()
            with open("/data/plex_understanding.jsonl", "a") as fh:
                fh.write(json.dumps(_report) + "\n")
        except Exception:
            pass
    return out



def sweep_orphan_downloads() -> dict:
    """Walk /Volumes/MediaVolume3/Downloads/music/complete and /Volumes/MediaVolume3/Downloads/music/spotiflac;
    for every audio file, read ID3 tags and move it into /Volumes/MediaVolume3/plexify-music/{Artist}/{Album}/{file}. Idempotent."""
    out = {"swept": 0, "failed": 0, "bytes": 0, "by_album": {}}
    global _SWEEP_LAST
    _now = time.time()
    if _now - _SWEEP_LAST < _SWEEP_MIN_INTERVAL:
        out["throttled"] = True
        return out
    _SWEEP_LAST = _now
    if len(_SWEEP_WARNED) > 5000:   # bound the warn-once set in the long-lived worker
        _SWEEP_WARNED.clear()
    DL_ROOTS = ["/Volumes/MediaVolume3/Downloads/music/complete", "/Volumes/MediaVolume3/Downloads/music/spotiflac"]
    MUSIC = "/Volumes/MediaVolume3/plexify-music"
    try:
        from mutagen import File as MutagenFile
    except ImportError:
        MutagenFile = None

    import shutil as _sh
    AUDIO = (".flac", ".mp3", ".m4a", ".alac", ".aac", ".ogg", ".wav", ".opus")

    def _safe(name, default="Unknown"):
        name = (name or "").strip().rstrip(". ")
        for ch in ["/", "\\", "<", ">", ":", '"', "|", "?", "*"]:
            name = name.replace(ch, "-")
        return (name[:200] or default).strip(". ")

    moved_paths_by_row: dict[tuple, list[str]] = {}
    _tk_cache: dict = {}   # dest_dir -> set of normalized song-title keys on disk
    def _dir_titles(d):
        if d not in _tk_cache:
            _tk_cache[d] = _folder_title_keys(d)
        return _tk_cache[d]
    source_by_row: dict[tuple, str] = {}  # (artist,album) -> 'soulseek'|'spotiflac' inferred from DL root
    for DL in DL_ROOTS:
        if not os.path.isdir(DL):
            continue
        for root, _d, files in os.walk(DL):
            for fn in files:
                if not fn.lower().endswith(AUDIO):
                    continue
                sp = os.path.join(root, fn)
                # In-flight guard: a fresh file may still be mid-download by the
                # picker's own mover — leave anything younger than 10 minutes.
                try:
                    if time.time() - os.path.getmtime(sp) < 600:
                        continue
                except OSError:
                    continue
                # AUDIT-LIKED: skip any file under a hidden/system-style dir
                # (path components starting with _ or .) — quarantine, attic, etc.
                rel_parts = os.path.relpath(sp, DL).split(os.sep)
                if any(p.startswith(('_', '.')) for p in rel_parts):
                    continue
                artist, album = "", ""
                if MutagenFile:
                    try:
                        m = MutagenFile(sp, easy=True)
                        if m and m.tags:
                            artist = (m.tags.get("albumartist") or m.tags.get("artist") or [""])[0]
                            album = (m.tags.get("album") or [""])[0]
                    except Exception:
                        pass
                # Fallback: derive from path (peer's directory structure).
                # NOTE: we DO NOT fall back to "Unknown Artist" — the user
                # prefers a file sit in /Volumes/MediaVolume3/Downloads/music forever over polluting
                # /Volumes/MediaVolume3/plexify-music with mis-attributed entries.
                if not artist or not album:
                    parts = os.path.relpath(sp, DL).split(os.sep)
                    if len(parts) >= 3:
                        artist = artist or parts[-3]
                        album = album or parts[-2]
                    elif len(parts) >= 2:
                        # Only the album dir is identifiable; artist unknown.
                        # SKIP rather than falsely attribute.
                        album = album or parts[-2]
                # FILENAME RESCUE: SpotiFLAC names loose singles "Title - Artist.ext"
                # (and sometimes "Artist - Title"). Trust the pattern ONLY when one
                # side exactly matches an artist we already know from the user's
                # playlists/queue — no guessing.
                if not artist or not artist.strip():
                    _base = os.path.splitext(fn)[0]
                    if " - " in _base:
                        _l, _, _r = _base.rpartition(" - ")
                        _ka = _known_artists()
                        if _r.strip().lower() in _ka:
                            artist = _r.strip()
                        elif _l.strip().lower() in _ka:
                            artist = _l.strip()
                # INTENT GATE: only import songs the user actually asked for
                # (liked/playlist titles) or files joining an album the pipeline
                # is already building — strays stay in downloads instead of
                # manifesting as accidental albums.
                _ik = _file_title_key(sp, artist or "")
                # INTENT GATE — the title must be wanted AND from a matching artist. A
                # wrong-artist VERSION of a liked song (e.g. Steve Winwood's live
                # "Dear Mr. Fantasy" vs the liked Traffic studio cut) shares the title but
                # no artist token; importing it would hijack the liked song's Plex
                # title-match. Collabs ("Coolio & L.V." vs "Coolio") share a token -> ok.
                _intent_ok = False
                if _ik and _ik in _intent_title_keys():
                    _want_tok = _intent_title_artists().get(_ik) or set()
                    _have_tok = _artist_tokens(artist)
                    _intent_ok = (not _want_tok) or (not _have_tok) or bool(_want_tok & _have_tok)
                if _ik and not _intent_ok:
                    _maybe_dir = os.path.join(MUSIC, _safe(artist or "", ""), _safe(album or "", "Unknown Album"))
                    if not os.path.isdir(_maybe_dir):
                        if sp not in _SWEEP_WARNED:
                            _SWEEP_WARNED.add(sp)
                            log.info("sweep: skipping unrequested song (no intent match): %s", sp)
                        out.setdefault("skipped_unrequested", 0)
                        out["skipped_unrequested"] += 1
                        continue
                # Skip files we can't confidently attribute to an artist.
                if not artist or not artist.strip() or artist.strip().lower() in ("unknown", "unknown artist", "various artists", "various", "va"):
                    if sp not in _SWEEP_WARNED:
                        _SWEEP_WARNED.add(sp)
                        log.warning("sweep: skipping unattributable file (no artist): %s", sp)
                    out.setdefault("skipped_no_artist", 0)
                    out["skipped_no_artist"] += 1
                    continue
                if not album or not album.strip():
                    # NO 'Unknown Album' imports — find the real album from the
                    # local Spotify mirror, or leave the file for a smarter pass.
                    _tguess = ""
                    if MutagenFile:
                        try:
                            m2 = MutagenFile(sp, easy=True)
                            _tguess = (m2.tags.get("title") or [""])[0] if m2 and m2.tags else ""
                        except Exception:
                            pass
                    if not _tguess:
                        _b = os.path.splitext(fn)[0]
                        _tguess = _b.rpartition(" - ")[0] if " - " in _b else _b
                    album = _album_from_db(_tguess, artist)
                if not album or not album.strip():
                    if sp not in _SWEEP_WARNED:
                        _SWEEP_WARNED.add(sp)
                        log.warning("sweep: skipping file with no resolvable album: %s", sp)
                    out.setdefault("skipped_no_album", 0)
                    out["skipped_no_album"] += 1
                    continue
                artist_s = _safe(artist, "")
                album_s = _safe(album, "Unknown Album")
                if not artist_s:
                    if sp not in _SWEEP_WARNED:
                        _SWEEP_WARNED.add(sp)
                        log.warning("sweep: skipping file with empty artist after sanitization: %s", sp)
                    out.setdefault("skipped_no_artist", 0)
                    out["skipped_no_artist"] += 1
                    continue
                dest_dir = os.path.join(MUSIC, artist_s, album_s)
                try:
                    os.makedirs(dest_dir, exist_ok=True)
                    os.chmod(dest_dir, 0o775)
                    try: os.chmod(os.path.dirname(dest_dir), 0o775)
                    except Exception: pass
                except Exception:
                    continue
                dp = os.path.join(dest_dir, fn)
                _tkey = _file_title_key(sp, artist_s)
                if (os.path.exists(dp)
                        or dest_dir in _locked_album_dirs()      # FINAL stage: immutable
                        or (_tkey and _tkey in _dir_titles(dest_dir))):  # same song, other name
                    # Duplicate (or the album is locked) — park the staging copy
                    # in the attic (never delete) instead of importing it.
                    if (get_config("acq_recovery_enabled", "0") or "0") != "1":
                        continue
                    try:
                        _attic = os.path.join(DL, "_attic_dupes", os.path.relpath(sp, DL))
                        os.makedirs(os.path.dirname(_attic), exist_ok=True)
                        _sh.move(sp, _attic)
                        _log_recovery_move("dupe", sp, _attic)
                        out.setdefault("atticed_dupes", 0)
                        out["atticed_dupes"] += 1
                    except Exception:
                        pass
                    continue
                # INTEGRITY GATE: never import a half-finished file (they play
                # ~40s then cut). Corrupt -> attic, not the library.
                _ok_i, _bad_i = _verify_flac_integrity([sp])
                if _bad_i:
                    try:
                        _attic = os.path.join(DL, "_attic_corrupt", os.path.relpath(sp, DL))
                        os.makedirs(os.path.dirname(_attic), exist_ok=True)
                        _sh.move(sp, _attic)
                        _log_recovery_move("corrupt", sp, _attic)
                        out.setdefault("corrupt", 0)
                        out["corrupt"] += 1
                    except Exception:
                        pass
                    continue
                try:
                    sz = os.path.getsize(sp)
                    _sh.move(sp, dp)
                    os.chmod(dp, 0o664)
                    if _tkey:
                        _dir_titles(dest_dir).add(_tkey)
                    # Orphan-swept files are usually loose, untagged Soulseek
                    # peer files — stamp the album/artist so they don't land in
                    # Plex as "[Unknown Album]".
                    try:
                        _stamp_file_tags(dp, artist_s, album_s)
                    except Exception:
                        log.exception("sweep: tag-stamp failed for %s", dp)
                    out["swept"] += 1
                    out["bytes"] += sz
                    key = (artist_s, album_s)
                    out["by_album"][f"{artist_s} / {album_s}"] = out["by_album"].get(f"{artist_s} / {album_s}", 0) + 1
                    moved_paths_by_row.setdefault(key, []).append(dp)
                    # Provenance: /Volumes/MediaVolume3/Downloads/music/complete is Soulseek's dir;
                    # /Volumes/MediaVolume3/Downloads/music/spotiflac is SpotiFLAC's. First file wins.
                    source_by_row.setdefault(key, "soulseek" if "complete" in DL else "spotiflac")
                except Exception as e:
                    out["failed"] += 1
                    log.warning("sweep: %s -> %s failed: %s", sp, dp, e)

    # For each (artist, album) we swept, record it as imported in AutofillAction
    # if a matching row exists OR create one. Also trigger mirror_from_local.
    if moved_paths_by_row:
        from .db import AutofillAction
        affected_rows = []
        with session_scope() as s:
            for (a, b), paths in moved_paths_by_row.items():
                ak = _normalize_for_key(a)
                bk = _normalize_for_key(b)
                _sw_src = source_by_row.get((a, b))
                if _sw_src == "soulseek":
                    _sw_detail = "Soulseek (swept)"
                elif _sw_src == "spotiflac":
                    _sw_detail = f"SpotiFLAC {get_spotiflac_version()} (swept)"
                else:
                    _sw_detail = None
                row = s.scalar(
                    select(AutofillAction)
                    .where(AutofillAction.artist_key == ak)
                    .where(AutofillAction.album_key == bk)
                )
                if not row:
                    # AUDIT-4: ultra-fuzzy fallback when filesystem-derived name
                    # doesn't exact-match Spotify-derived name. Try matching on
                    # alphanumeric-only prefix overlap of >= half the shorter side.
                    def _alnum(z):
                        return "".join(c for c in (z or "").lower() if c.isalnum())
                    target_b = _alnum(bk)
                    candidates = list(s.scalars(
                        select(AutofillAction).where(AutofillAction.artist_key == ak)
                    ).all())
                    for cand in candidates:
                        cand_b = _alnum(cand.album_key)
                        if not cand_b or not target_b:
                            continue
                        n = min(len(cand_b), len(target_b))
                        prefix = max(8, n // 2)
                        if cand_b[:prefix] == target_b[:prefix]:
                            row = cand
                            log.info("sweep: fuzzy-matched orphan %s/%s -> existing row id=%d (%s)",
                                     a, b, cand.id, cand.album)
                            break
                if not row:
                    row = AutofillAction(
                        artist=a, album=b,
                        artist_key=ak, album_key=bk,
                        status="imported",
                        note=f"orphan sweep imported {len(paths)} files",
                        pre_existing_files=0,
                        source=_sw_src,
                        source_detail=_sw_detail,
                    )
                    s.add(row)
                    s.flush()
                elif row.status == "complete_locked":
                    continue  # FINAL stage — the row is immutable
                else:
                    row.status = "imported"
                    row.note = (f"orphan sweep imported {len(paths)} files")[:1024]
                    row.last_attempt_at = datetime.utcnow()
                    row.source = _sw_src or row.source
                    row.source_detail = _sw_detail or row.source_detail
                row.imported_paths = json.dumps(paths)
                row.total_size_bytes = sum(
                    os.path.getsize(p) for p in paths if os.path.exists(p)
                )
                affected_rows.append(row.id)
        # Trigger mirror outside the session so it can read the updated rows
        with session_scope() as s:
            for rid in affected_rows:
                row = s.get(AutofillAction, rid)
                if row:
                    try:
                        _trigger_mirror_for_imported_album(row)
                    except Exception:
                        log.exception("sweep: mirror trigger failed for row %d", rid)
    return out

def get_current_acquisitions() -> list[dict]:
    """All in-flight SpotiFLAC downloads (dashboard 'Right now' card reads this).
    Prunes zombie entries every call so the UI is self-healing."""
    _prune_stale_acquisitions()
    with _ACQUISITION_LOCK:
        return list(CURRENT_ACQUISITIONS.values())






def _ownership_attested() -> bool:
    """Legal-use attestation gate. No acquisition runs until the user has agreed (the setup
    wizard's Agreement step / the native app's gate). The authoritative enforcement lives in
    nas_downloader.enqueue_and_wait; these tick-level checks just no-op cleanly + surface a
    reason instead of spinning the queue against a blocked transport."""
    return (get_config("ownership_attested", "0") or "0") == "1"


def upgrade_quality_tick() -> dict:
    """Background sweeper: re-acquires LOSSLESS imports at HI_RES_LOSSLESS.

    Polite, batched: only fires when queue_depth == 0 and no in-flight
    acquisitions. Processes up to 3 albums per sweep.
    """
    result = {"considered": 0, "attempted": 0, "upgraded": 0, "skipped_busy": False}
    try:
        if not _ownership_attested():
            result["skipped_busy"] = True
            return result
        # Politeness gate: only run when picker queue is empty AND nothing in flight
        with _ACQUISITION_LOCK:
            in_flight = len(CURRENT_ACQUISITIONS)
        if in_flight > 0:
            result["skipped_busy"] = True
            return result

        from .db import SessionLocal, AutofillAction
        with SessionLocal() as s:
            queued = s.query(AutofillAction).filter(AutofillAction.status == "queued").count()
            if queued > 0:
                result["skipped_busy"] = True
                return result
            # Find candidates: imported rows where quality is LOSSLESS and not previously attempted
            candidates = (
                s.query(AutofillAction)
                .filter(
                    AutofillAction.status.in_(("imported", "complete_locked")),  # Part 4: upgrade even reorganized/locked albums
                    AutofillAction.quality_acquired == "LOSSLESS",
                    (AutofillAction.hires_upgrade_attempted == False) | (AutofillAction.hires_upgrade_attempted.is_(None)),
                )
                .order_by(AutofillAction.last_attempt_at.asc())
                .limit(3)
                .all()
            )
            result["considered"] = len(candidates)
            candidate_ids = [c.id for c in candidates]

        from .spotiflac_adapter import acquire as spotiflac_acquire
        from .db import SessionLocal, AutofillAction
        import os as _os, shutil as _shutil, json as _json, time as _time

        for cid in candidate_ids:
            with SessionLocal() as s:
                row = s.query(AutofillAction).filter(AutofillAction.id == cid).first()
                if not row:
                    continue
                artist = row.artist
                album = row.album
                spotify_url = None
                # Try to construct album URL from foreign_album_id, else from track_ids
                if row.foreign_album_id and row.foreign_album_id.startswith("sp:"):
                    spotify_url = f"https://open.spotify.com/album/{row.foreign_album_id[3:]}"
                if not spotify_url and row.track_ids_json:
                    try:
                        tids = _json.loads(row.track_ids_json) or []
                        if tids:
                            spotify_url = f"https://open.spotify.com/track/{tids[0]}"
                    except Exception:
                        pass
                imported_paths = []
                try:
                    imported_paths = _json.loads(row.imported_paths or "[]") or []
                except Exception:
                    pass

            if not spotify_url:
                with SessionLocal() as s:
                    r = s.query(AutofillAction).filter(AutofillAction.id == cid).first()
                    if r:
                        r.hires_upgrade_attempted = True
                        s.commit()
                continue

            result["attempted"] += 1
            tmp_dir = f"/Volumes/MediaVolume3/Downloads/music/spotiflac/_upgrade_{cid}_{int(_time.time())}"
            try:
                _os.makedirs(tmp_dir, exist_ok=True)
            except Exception:
                pass
            log.info("upgrade_quality_tick: trying hi-res for row=%d %s/%s", cid, artist, album)
            res = spotiflac_acquire(
                spotify_url,
                dest_dir=tmp_dir,
                quality="HI_RES_LOSSLESS",
                timeout_seconds=180,
            )
            success = bool(res.success and res.paths)
            # Verify actually hi-res via mutagen
            verified = []
            if success:
                try:
                    from mutagen.flac import FLAC
                    for p in (res.paths or []):
                        try:
                            f = FLAC(p)
                            if (f.info.bits_per_sample or 0) >= 24 and (f.info.sample_rate or 0) >= 96000:
                                verified.append(p)
                        except Exception:
                            pass
                except Exception:
                    pass
            if success and verified and len(verified) >= max(1, len(res.paths) - 1):
                # Replace old files with new — best-effort
                # L-2 safety: defer deletion of old files until AFTER move succeeds.
                # (We stash them here; delete only on confirmed upgrade.)
                # Part 4: the album rules relocate files (and old rows store the pre-rename
                # original root), so re-root each old path to the live prefix — otherwise the
                # stale lossless original survives the upgrade as a duplicate.
                _old_paths_to_delete = []
                for _op in imported_paths:
                    for _old in ("/plexify-music/",):
                        if _op.startswith(_old):
                            _op = "/Volumes/MediaVolume3/plexify-music/" + _op[len(_old):]; break
                    _old_paths_to_delete.append(_op)
                # 2) Move new files to /Volumes/MediaVolume3/plexify-music/{Artist}/{Album}/ — same destination
                #    pattern picker_tick uses. _tag_based_placement only VERIFIES, it
                #    does not move files (returns a (passing, failing) tuple).
                moved = []
                try:
                    safe_artist = _safe_for_fs(artist)
                    safe_album  = _safe_for_fs(album)
                    music_root = "/Volumes/MediaVolume3/plexify-music"
                    dest_dir = _os.path.join(music_root, safe_artist, safe_album)
                    _os.makedirs(dest_dir, exist_ok=True)
                    import shutil as _shutil_up
                    for src_p in verified:
                        try:
                            if not _os.path.isfile(src_p):
                                continue
                            dest_p = _os.path.join(dest_dir, _os.path.basename(src_p))
                            if _os.path.exists(dest_p):
                                # Already in place — count it and continue.
                                moved.append(dest_p); continue
                            tmp_p = dest_p + ".tmp_upgrade"
                            _shutil_up.copy2(src_p, tmp_p)
                            _os.replace(tmp_p, dest_p)
                            try: _os.remove(src_p)
                            except Exception: pass
                            moved.append(dest_p)
                        except Exception:
                            log.exception("upgrade_quality_tick: move failed for %s", src_p)
                except Exception:
                    log.exception("upgrade_quality_tick: dest-dir setup failed")
                if moved and len(moved) >= max(1, len(verified) // 2):
                    # Now safe: move succeeded, delete the old LOSSLESS originals.
                    for _old_p in _old_paths_to_delete:
                        try:
                            if _os.path.isfile(_old_p) and _old_p not in moved:
                                _os.remove(_old_p)
                        except Exception:
                            log.exception("upgrade_quality_tick: failed to remove old %s", _old_p)
                    with SessionLocal() as s:
                        r = s.query(AutofillAction).filter(AutofillAction.id == cid).first()
                        if r:
                            r.quality_acquired = "HI_RES_LOSSLESS"
                            r.hires_upgrade_attempted = True
                            try:
                                r.imported_paths = _json.dumps(moved)
                            except Exception:
                                pass
                            s.commit()
                    result["upgraded"] += 1
                    log.info("upgrade_quality_tick: UPGRADED row=%d files=%d", cid, len(moved))
                else:
                    log.warning("upgrade_quality_tick: hi-res files acquired but placement failed for row=%d", cid)
                    with SessionLocal() as s:
                        r = s.query(AutofillAction).filter(AutofillAction.id == cid).first()
                        if r:
                            r.hires_upgrade_attempted = True
                            s.commit()
            else:
                # No hi-res available — mark attempted so we don't re-try
                with SessionLocal() as s:
                    r = s.query(AutofillAction).filter(AutofillAction.id == cid).first()
                    if r:
                        r.hires_upgrade_attempted = True
                        s.commit()
                log.info("upgrade_quality_tick: NO hi-res available for row=%d", cid)
            # Clean up tmp dir
            try:
                if _os.path.isdir(tmp_dir):
                    _shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass
    except Exception:
        log.exception("upgrade_quality_tick: unexpected failure")
    log.info("upgrade_quality_tick: %s", result)
    return result


def _safe_for_fs(name, default="Unknown"):
    """Module-level twin of the picker's local path sanitizer. Re-acquire sweeps
    (quality upgrade + album completion) MUST write into the *same* folders the
    picker created, so this is byte-for-byte identical to the nested helper in
    picker_tick. (Previously upgrade_quality_tick referenced a name that only
    existed as a picker-local, so its move block silently NameError'd — defining
    it here fixes that latent bug too.)"""
    name = (name or "").strip().rstrip(". ")
    for ch in ["/", "\\", "<", ">", ":", '"', "|", "?", "*"]:
        name = name.replace(ch, "-")
    return (name[:200] or default).strip(". ")


# ---------------------------------------------------------------------------
# Album-completion sweeper — "try harder to complete albums the less is left"
# ---------------------------------------------------------------------------
# Finds imported albums that are PARTIAL (fewer downloaded tracks than the real
# album has per the local Spotify-catalog mirror) and re-acquires the FULL album
# IN PLACE: it merges only the *new* tracks into the album's existing /Volumes/MediaVolume3/plexify-music
# folder, never touches what's already there, and keeps status='imported' so the
# album never disappears from the Albums page while it's being topped up.
#
# Effort scales with how close an album is to done. The cooldown between attempts
# is proportional to how many tracks are still missing, so an album that's 1 track
# short is retried ~every 20 min while one that's 10 short waits a few hours.
# Fewest-missing albums are also tried first each tick. That is the literal reading
# of "make it try harder to complete albums the less songs are left in the album."
#
# Politeness: yields entirely while a video is streaming (movie playback wins) and
# only runs when a download lane is free, so it never steals slots from the picker.
# Album completion is the "filler" half of downloads. Was single-flight; now a small
# bounded semaphore so completions can run on ~half the picker slots in parallel — the
# atomic per-album claim in the selection below stops two runs grabbing the same album.
_COMPLETE_ALBUMS_SEM = threading.BoundedSemaphore(6)

# @BeatSpotBot is ONE linear chat on ONE user account, so its searches must never
# overlap (concurrent queries would interleave replies). This 1-permit semaphore
# serializes ALL Telegram acquisitions app-wide — both the picker's third-source
# fallback and the per-track album completion below. Callers use it non-blocking and
# simply skip Telegram for that turn when it's busy (retried on a later tick).
_TELEGRAM_SEM = threading.BoundedSemaphore(1)
# Per completion tick, how many missing tracks to pull from Telegram at most — keeps a
# single album from monopolizing the bot for minutes. The album is revisited next tick.
_TG_COMPLETE_MAX_PER_TICK = 4


def _tg_title_key(s: str) -> str:
    """Normalized title key for dedup/matching: drop (remix)/(feat …) qualifiers first."""
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s or "")
    return _normalize_for_key(s)


def _fname_title_key(path: str, artist: str = "", album: str = "") -> str:
    """Best-effort title key derived from a FILENAME — the fallback for source files
    that ship with no TITLE tag (some Soulseek peers). Strips leading track numbers and
    any artist/album tokens, so e.g. '03 - The Less I Know the Better.flac' → the same key
    as the mirror's track title. Garbage names (ISRC ids) yield a non-matching key, which
    harmlessly just doesn't dedup anything."""
    import os as _os
    b = _os.path.splitext(_os.path.basename(path or ""))[0]
    for tok in (artist, album):                               # drop "Artist - " / "Album"
        if tok and len(tok) >= 3:
            b = re.sub(re.escape(tok), " ", b, flags=re.IGNORECASE)
    b = re.sub(r"\b(flac|cd\d?|disc\d?|\d{2,3}kbps)\b", " ", b, flags=re.IGNORECASE)
    b = b.strip(" -_.)(][")
    b = re.sub(r"^\s*\d{1,3}\s*[-._)\]]?\s*", "", b)          # leading "01 - " / "01." / "01)"
    return _tg_title_key(b)


def _force_title_tag(path: str, title: str, tracknumber=None) -> bool:
    """FORCE-write the song title (and track number) onto a FLAC. Used for Telegram-sourced
    files, whose embedded title is the ISRC code — so Plex shows the real name and our
    title-based dedup matches on future runs. Best-effort; never raises into placement."""
    try:
        from mutagen.flac import FLAC
        f = FLAC(path)
        if title:
            f["title"] = title
        if tracknumber:
            f["tracknumber"] = str(tracknumber)
        f.save()
        return True
    except Exception:
        log.exception("_force_title_tag failed for %s", path)
        return False


def _telegram_fill_album_tracks(*, cid, artist, album, fa, track_ids,
                                have_paths, dest_dir, total, tmp_dir):
    """Fill an album's MISSING tracks one-by-one from @BeatSpotBot (FLAC) and merge them.

    Dedups by track TITLE (read from embedded tags), NOT filename — so a track we already
    hold under a SpotiFLAC/Soulseek name is never re-downloaded under the bot's ISRC name
    (the "repeat downloads" the user flagged). Bounded per tick and gated on streaming.
    Returns the list of newly-merged file paths.
    """
    import os as _os, shutil as _shutil
    from .db import SessionLocal, SpotifyAlbum, SpotifyAlbumTrack
    from . import telegram_picker as _tgp

    # 1) Resolve the album's track listing from the mirror (titles + order).
    album_id = fa[3:] if (fa or "").startswith("sp:") else None
    titles = []  # (position, title)
    with SessionLocal() as s:
        rows = []
        if album_id:
            rows = (s.query(SpotifyAlbumTrack)
                     .filter(SpotifyAlbumTrack.album_id == album_id)
                     .order_by(SpotifyAlbumTrack.position).all())
        if not rows:
            # Fall back: find the album by (artist, name) key, then its tracks.
            akey = _normalize_for_key(artist or "")
            nkey = _normalize_for_key(album or "")
            for a in s.query(SpotifyAlbum).filter(SpotifyAlbum.album_artist_key == akey).all():
                if _normalize_for_key(a.name or "") == nkey:
                    album_id = a.album_id
                    rows = (s.query(SpotifyAlbumTrack)
                             .filter(SpotifyAlbumTrack.album_id == album_id)
                             .order_by(SpotifyAlbumTrack.position).all())
                    break
        for t in rows:
            if t.title:
                titles.append((t.position or 0, t.title))
    if not titles:
        return []

    # 2) Which track titles do we already have on disk (by embedded TITLE tag)?
    have_keys = set()
    for p in have_paths:
        try:
            tg = _read_file_tags(p)
            if tg.get("title"):
                have_keys.add(_tg_title_key(tg["title"]))
        except Exception:
            pass
        # Fallback for files with no TITLE tag (some Soulseek peers) so we never
        # re-fetch — under the bot's ISRC name — a track we already hold.
        try:
            fk = _fname_title_key(p, artist, album)
            if fk and len(fk) >= 3:
                have_keys.add(fk)
        except Exception:
            pass

    # 3) Fill each missing title, bounded. Per track, try Soulseek first (free, peer-to-peer,
    # loosened song search + smart file matcher), then @BeatSpotBot. The delivered file is
    # merged below regardless of which source produced it.
    moved = []
    sources_used = set()
    budget = _TG_COMPLETE_MAX_PER_TICK
    for _pos, title in titles:
        if budget <= 0 or (len(have_paths) + len(moved)) >= total:
            break
        if _tg_title_key(title) in have_keys:
            continue
        # Politeness: yield mid-batch if a video stream starts.
        try:
            if get_config("autofill_pause_when_streaming", "1") == "1" and _plex_active_video_sessions() > 0:
                break
        except Exception:
            pass
        budget -= 1
        src = None
        src_kind = None
        # (a0) squid.wtf (Qobuz hi-res) — most reliable; tried first.
        try:
            from . import squid_adapter as _sqa
            if _sqa.is_enabled() and not _sqa._in_break():
                _sqr = _sqa.acquire_track(artist=artist, title=title, dest_dir=tmp_dir,
                                          flac_only=True, timeout_seconds=70)
                if _sqr and _sqr.get("success") and _sqr.get("paths"):
                    src = _sqr["paths"][0]
                    src_kind = "squid"
        except Exception:
            log.exception("album fill: squid acquire_track crashed for %s - %s", artist, title)
        # (a) Soulseek song-level — loosened search + free smart matcher.
        try:
            from . import slskd_picker as _slp
            if _slp.configured() and not src:
                _sr = _slp.acquire_track(artist=artist, title=title, flac_only=True, timeout_seconds=90)
                if _sr and _sr.get("success") and _sr.get("paths"):
                    src = _sr["paths"][0]
                    src_kind = "soulseek"
        except Exception:
            log.exception("album fill: soulseek acquire_track crashed for %s - %s", artist, title)
        # (b) Telegram fallback (@BeatSpotBot).
        if not src:
            try:
                r = _tgp.acquire(artist=artist, sample_song=title, album=album,
                                 dest_dir=tmp_dir, flac_only=True, timeout_seconds=120)
                if r and r.get("success") and r.get("paths"):
                    src = r["paths"][0]
                    src_kind = "telegram"
            except Exception:
                log.exception("album fill: telegram acquire crashed for %s - %s", artist, title)
        if not src:
            continue
        # GLOBAL song dedup: never fill a song we already have ANYWHERE (another album /
        # compilation) unless this copy is a strict quality upgrade. Deletes only the temp.
        _si2 = ""
        _srk2 = 0
        try:
            _sa2, _st2, _si2, _srk2 = _file_song_info(src)
            if _dedup_enabled():
                with SessionLocal() as _ds:
                    _have2 = _ledger_have_quality(_ds, artist or _sa2, title or _st2, _si2)
                if _have2 is not None and _have2 >= _srk2:
                    try: _os.remove(src)
                    except Exception: pass
                    continue
        except Exception:
            pass
        # NOTE: we do NOT validate against the file's title tag — @BeatSpotBot tags its
        # FLACs with the ISRC code, not the song name. telegram_picker already matched the
        # bot's RESULT-LISTING title to `title` (strict ≥55 score), so the selection is the
        # trustworthy signal. We then FORCE the real title on the file below.
        try:
            # Name by the real title (NOT the bot's ISRC filename), matching the album's
            # "NN - Title.flac" convention. Bonus: this collides with an existing same-track
            # file → dedup, instead of landing as an ISRC-named duplicate.
            _safe_t = re.sub(r'[/\\:*?"<>|]', '-', title or '').strip().rstrip('.')[:120]
            _ext = _os.path.splitext(src)[1] or ".flac"
            _nm = (f"{int(_pos):02d} - {_safe_t}{_ext}" if (_pos and _safe_t) else
                   (f"{_safe_t}{_ext}" if _safe_t else _os.path.basename(src)))
            dest_p = _os.path.join(dest_dir, _nm)
            if _os.path.exists(dest_p):
                try: _os.remove(src)
                except Exception: pass
                continue
            tmp_p = dest_p + ".tmp_tgfill"
            _shutil.copy2(src, tmp_p)
            _os.replace(tmp_p, dest_p)
            try: _os.chmod(dest_p, 0o664)
            except Exception: pass
            try: _os.remove(src)
            except Exception: pass
            # Stamp album/artist (fills blanks) AND force the correct song title +
            # track number — otherwise Plex would show the ISRC as the track name, and
            # future dedup-by-title (above) would never match this file.
            try:
                _aa, _aa_conf = _canonical_album_artist(artist, album, track_ids, fa)
                _stamp_file_tags(dest_p, artist, album, album_artist=(_aa if _aa_conf else None))
            except Exception:
                pass
            try:
                _force_title_tag(dest_p, title, _pos)
            except Exception:
                pass
            # Provenance: the real per-track source (squid / soulseek / telegram).
            try:
                _set_source_tag(dest_p, src_kind)
            except Exception:
                pass
            moved.append(dest_p)
            have_keys.add(_tg_title_key(title))
            # Teach the global ledger so this song is never fetched again (non-upgrading).
            try:
                with SessionLocal() as _rs:
                    _ledger_remember(_rs, artist, title, _si2,
                                     _srk2 or _quality_rank(_actual_flac_quality([dest_p])),
                                     path=dest_p)
                    _rs.commit()
            except Exception:
                pass
            if src_kind:
                sources_used.add(src_kind)
        except Exception:
            log.exception("album fill: merge failed for %s", src)

    if moved:
        for _k in sources_used:
            try:
                _record_provider_success(_k)
            except Exception:
                pass
        log.info("album fill: row=%d +%d track(s) for %s / %s (sources=%s)",
                 cid, len(moved), artist, album, sorted(sources_used))
    return moved


def complete_albums_tick(batch: int = 1) -> dict:
    result = {"considered": 0, "attempted": 0, "completed_more": 0,
              "tracks_added": 0, "telegram_tracks_added": 0,
              "skipped_busy": False, "skipped_streaming": False}
    # Bounded concurrency: several completions may run at once (the picker's filler
    # turns drive them), capped by the semaphore to avoid hammering rate-limited sources.
    if not _COMPLETE_ALBUMS_SEM.acquire(blocking=False):
        result["skipped_busy"] = True
        return result
    try:
        # Gate 1: never compete with active video playback (high-bitrate movies win).
        if get_config("autofill_pause_when_streaming", "1") == "1":
            try:
                if _plex_active_video_sessions() > 0:
                    result["skipped_streaming"] = True
                    log.info("complete_albums_tick: %s", result)
                    return result
            except Exception:
                pass
        # NOTE: deliberately NOT gated on picker lane saturation. The picker keeps all
        # lanes full as its steady state, so a "free lane" gate would mean completion
        # never runs at all. Instead this sweeper is bounded to be gentle by design:
        # scheduler max_instances=1 + sequential batch ⇒ at most ONE extra concurrent
        # download, default one album every 4 min, and it yields entirely while a video
        # streams (gate above). That's the intended "try harder to complete albums."
        from .db import SessionLocal, AutofillAction, SpotifyAlbum
        from .spotify_catalog import best_album_for_liked as _best_album_for_liked
        import os as _os, shutil as _shutil, json as _json, time as _time
        from datetime import datetime as _dt, timezone as _tz

        now = _dt.now(_tz.utc)

        # Mirror total-track lookups — same source of truth as the Albums page.
        by_id, by_name = {}, {}
        picked_meta = []  # (id, total)
        with SessionLocal() as s:
            for a in s.query(SpotifyAlbum).all():
                try:
                    tt = int(a.total_tracks or 0)
                except Exception:
                    tt = 0
                if tt <= 0:
                    continue
                by_id[a.album_id] = tt
                by_name[(_normalize_for_key(a.album_artist or ""), _normalize_for_key(a.name or ""))] = tt

            rows = (
                s.query(AutofillAction)
                .filter(AutofillAction.status == "imported",
                        AutofillAction.imported_paths.isnot(None))
                .all()
            )
            cands = []  # (missing, last_attempt_sort, id, total)
            epoch = _dt(1970, 1, 1, tzinfo=_tz.utc)
            try:
                from . import spotify_catalog as _sc_blk2
            except Exception:
                _sc_blk2 = None
            for r in rows:
                try:
                    _ipaths = _json.loads(r.imported_paths or "[]") or []
                except Exception:
                    _ipaths = []
                have = len(_ipaths)
                # DISK TRUTH: files added by other paths (recovery, sweep,
                # consolidation) never update this row's imported_paths, so the
                # JSON undercounts and completion re-downloads songs that are
                # already there. Count the album folder itself.
                if _ipaths:
                    _fold = _os.path.dirname(_ipaths[0])
                    try:
                        _AUD = (".flac", ".mp3", ".m4a", ".alac", ".aac", ".ogg", ".opus", ".wav")
                        _n = sum(1 for f in _os.listdir(_fold) if f.lower().endswith(_AUD))
                        if _n > 0:
                            have = max(have, _n)
                    except OSError:
                        pass
                if have < 1:
                    continue
                # Never grow a generic hits compilation ("Now That's What I Call Music!" etc.).
                if _sc_blk2 is not None and _sc_blk2.is_blocked_comp_name(r.album or ""):
                    continue
                total = None
                fa = (r.foreign_album_id or "")
                if fa.startswith("sp:"):
                    total = by_id.get(fa[3:])
                if total is None:
                    total = by_name.get((_normalize_for_key(r.artist or ""), _normalize_for_key(r.album or "")))
                if not total:
                    continue
                if int(total) > 50:
                    # RULE 4: an album is never built out from a counterpart of
                    # more than 50 songs — no filling, no locking. The rules
                    # tick re-homes its liked songs into smaller counterparts.
                    continue
                if have >= total:
                    # FINAL STAGE: the album is complete — lock it. Locked albums
                    # leave the imported pool entirely (completion + quality
                    # upgrades only select status='imported'), their folders are
                    # immutable to the movers, and their songs are never
                    # re-attempted.
                    r.status = "complete_locked"
                    r.note = ("complete — locked (%d/%d tracks)" % (have, total))[:1024]
                    result["locked"] = result.get("locked", 0) + 1
                    continue  # unknown total or already complete
                # Exhausted: this album's remaining tracks are all songs we already have
                # (global dedup keeps skipping them), so it can never progress — stop
                # re-downloading it. Kill-switch: autofill_complete_zero_cap=0.
                try:
                    _zcap = int(get_config("autofill_complete_zero_cap", "3") or "3")
                except Exception:
                    _zcap = 3
                if _zcap > 0 and int(getattr(r, "complete_zero_streak", 0) or 0) >= _zcap:
                    continue
                missing = total - have
                # Cooldown ∝ missing → fewer missing = retried harder / more often.
                cooldown_min = max(15, min(360, missing * 20))
                last = r.complete_attempt_at
                if last is not None:
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=_tz.utc)
                    if (now - last).total_seconds() < cooldown_min * 60:
                        continue  # still cooling down
                    sort_attempt = last
                else:
                    sort_attempt = epoch
                cands.append((missing, sort_attempt, r.id, total, r.complete_attempt_at))
            result["considered"] = len(cands)
            try:
                s.commit()  # persist complete_locked flips from the scan above
            except Exception:
                s.rollback()
            # Fewest-missing first, then least-recently-attempted.
            cands.sort(key=lambda c: (c[0], c[1]))
            # ATOMIC CLAIM: stamp complete_attempt_at only if it's unchanged since we
            # read it (optimistic lock). Concurrent completion runs therefore can't both
            # grab the same album — the loser's UPDATE matches 0 rows and it moves on.
            for (_m, _la, cid, total, raw_last) in cands:
                guard = (AutofillAction.complete_attempt_at.is_(None) if raw_last is None
                         else (AutofillAction.complete_attempt_at == raw_last))
                res = s.execute(
                    update(AutofillAction).where(AutofillAction.id == cid).where(guard)
                    .values(complete_attempt_at=now)
                )
                if res.rowcount == 1:
                    picked_meta.append((cid, total))
                    if len(picked_meta) >= max(1, batch):
                        break
            s.commit()

        if not picked_meta:
            log.info("complete_albums_tick: %s", result)
            return result

        from .spotiflac_adapter import acquire as spotiflac_acquire

        for cid, total in picked_meta:
            with SessionLocal() as s:
                row = s.query(AutofillAction).filter(AutofillAction.id == cid).first()
                if not row:
                    continue
                artist = row.artist
                album = row.album
                try:
                    track_ids = _json.loads(row.track_ids_json or "[]") or []
                except Exception:
                    track_ids = []
                try:
                    imported_paths = _json.loads(row.imported_paths or "[]") or []
                except Exception:
                    imported_paths = []
                fa = (row.foreign_album_id or "")
                quality = row.quality_acquired or (
                    "LOSSLESS" if (get_config("autofill_allow_cd_quality", "0") == "1") else "HI_RES_LOSSLESS"
                )

            # Resolve the FULL album URL: foreign id first, else via the mirror.
            album_url = None
            if fa.startswith("sp:"):
                album_url = f"https://open.spotify.com/album/{fa[3:]}"
            if not album_url:
                for tid in track_ids:
                    try:
                        best = _best_album_for_liked(tid)
                    except Exception:
                        best = None
                    if best and best.get("album_url"):
                        album_url = best["album_url"]
                        break

            def _stamp(commit_progress=None):
                """Record the attempt (always) so the cooldown is honoured even on no-op."""
                with SessionLocal() as s:
                    r = s.query(AutofillAction).filter(AutofillAction.id == cid).first()
                    if not r:
                        return
                    r.complete_attempt_at = _dt.now(_tz.utc)
                    r.complete_attempts = (r.complete_attempts or 0) + 1
                    if commit_progress:
                        merged, added_bytes = commit_progress
                        try:
                            r.imported_paths = _json.dumps(merged)
                        except Exception:
                            pass
                        r.total_size_bytes = (r.total_size_bytes or 0) + added_bytes
                    s.commit()

            if not album_url:
                _stamp()
                continue

            result["attempted"] += 1
            tmp_dir = f"/Volumes/MediaVolume3/Downloads/music/spotiflac/_complete_{cid}_{int(_time.time())}"
            try:
                _os.makedirs(tmp_dir, exist_ok=True)
            except Exception:
                pass

            log.info("complete_albums_tick: filling %s / %s (have=%d total=%d) via %s",
                     artist, album, len(imported_paths), total, album_url)
            try:
                res = spotiflac_acquire(album_url, dest_dir=tmp_dir, quality=quality, timeout_seconds=180)
                if (not res or not res.success or not res.paths) and quality == "HI_RES_LOSSLESS":
                    res = spotiflac_acquire(album_url, dest_dir=tmp_dir, quality="LOSSLESS", timeout_seconds=180)
            except Exception:
                log.exception("complete_albums_tick: acquire failed for row=%d", cid)
                res = None

            new_paths = list(res.paths) if (res and res.success and res.paths) else []

            # Merge ONLY tracks we don't already have into the existing album folder.
            existing_basenames = {_os.path.basename(p) for p in imported_paths}
            moved = []
            if new_paths:
                try:
                    # Merge into the album's EXISTING folder (where its files already
                    # live) so we never re-split a consolidated album. Only derive a
                    # fresh path if somehow there are no existing files to anchor on.
                    if imported_paths:
                        dest_dir = _os.path.dirname(imported_paths[0])
                    else:
                        _aa, _ = _canonical_album_artist(
                            artist, album, track_ids, fa)
                        dest_dir = f"/Volumes/MediaVolume3/plexify-music/{_safe_for_fs(_aa, 'Unknown Artist')}/{_safe_for_fs(album, 'Unknown Album')}"
                    try:
                        _os.makedirs(dest_dir, exist_ok=True)
                        _os.chmod(dest_dir, 0o775)
                    except Exception:
                        pass
                    for src_p in new_paths:
                        try:
                            if not _os.path.isfile(src_p):
                                continue
                            base = _os.path.basename(src_p)
                            dest_p = _os.path.join(dest_dir, base)
                            if base in existing_basenames or _os.path.exists(dest_p):
                                continue  # already have this exact track
                            # SAME-SONG GATE: the song may already be on disk under a
                            # different filename convention — compare TITLES, not names.
                            if _file_title_key(src_p, artist) in _folder_title_keys(dest_dir, artist):
                                result["skipped_present"] = result.get("skipped_present", 0) + 1
                                try: _os.remove(src_p)
                                except Exception: pass
                                continue
                            # INTEGRITY GATE: half-finished/corrupt downloads cut off
                            # mid-playback — decode-verify before letting one in.
                            _ok_i, _bad_i = _verify_flac_integrity([src_p])
                            if _bad_i:
                                result["skipped_corrupt"] = result.get("skipped_corrupt", 0) + 1
                                try: _os.remove(src_p)
                                except Exception: pass
                                continue
                            # GLOBAL song dedup: a song we already have ANYWHERE (e.g. a track
                            # shared by several compilations) is not downloaded again unless it's
                            # a strict quality upgrade. Only ever deletes the temp download.
                            with SessionLocal() as _ds:
                                _is_dupe, _sa, _st, _si, _srk = _ledger_is_dupe(_ds, src_p)
                            if _is_dupe:
                                result["skipped_dupe"] = result.get("skipped_dupe", 0) + 1
                                try: _os.remove(src_p)
                                except Exception: pass
                                continue
                            tmp_p = dest_p + ".tmp_complete"
                            _shutil.copy2(src_p, tmp_p)
                            _os.replace(tmp_p, dest_p)
                            try: _os.chmod(dest_p, 0o664)
                            except Exception: pass
                            try: _os.remove(src_p)
                            except Exception: pass
                            moved.append(dest_p)
                            existing_basenames.add(base)
                            # Teach the ledger (real track artist/title/ISRC from the file's tags,
                            # captured BEFORE the album-artist stamp below).
                            try:
                                with SessionLocal() as _rs:
                                    _ledger_remember(_rs, _sa or artist, _st, _si, _srk,
                                                     path=dest_p)
                                    _rs.commit()
                            except Exception:
                                pass
                        except Exception:
                            log.exception("complete_albums_tick: move failed for %s", src_p)
                    # Stamp the canonical album artist on the newly merged tracks so
                    # they group with the album already in this folder (not split off
                    # under the performer credit).
                    if moved:
                        _aa, _aa_conf = _canonical_album_artist(artist, album, track_ids, fa)
                        for _mp in moved:
                            try:
                                _stamp_file_tags(_mp, artist, album,
                                                 album_artist=(_aa if _aa_conf else None))
                            except Exception:
                                log.exception("complete_albums_tick: tag-stamp failed for %s", _mp)
                            # Completion downloads come from SpotiFLAC.
                            try:
                                _set_source_tag(_mp, "spotiflac")
                            except Exception:
                                pass
                except Exception:
                    log.exception("complete_albums_tick: dest-dir setup failed")

            # ── Telegram per-track completion (same smart fill turn). Fills whatever
            # SpotiFLAC couldn't — or the whole remainder when SpotiFLAC is down — by
            # searching @BeatSpotBot for each MISSING track and grabbing its FLAC.
            # Serialized via the 1-permit semaphore (single bot chat); skipped when busy.
            try:
                from . import telegram_picker as _tgp_chk
                _tg_ready = _tgp_chk.is_configured()
            except Exception:
                _tg_ready = False
            try:
                if _tg_ready and (len(imported_paths) + len(moved)) < total:
                    if _TELEGRAM_SEM.acquire(blocking=False):
                        try:
                            _tg_dest = _os.path.dirname(imported_paths[0]) if imported_paths else None
                            if _tg_dest:
                                tg_moved = _telegram_fill_album_tracks(
                                    cid=cid, artist=artist, album=album, fa=fa,
                                    track_ids=track_ids,
                                    have_paths=(list(imported_paths) + moved),
                                    dest_dir=_tg_dest, total=total, tmp_dir=tmp_dir)
                                if tg_moved:
                                    moved.extend(tg_moved)
                                    result["telegram_tracks_added"] += len(tg_moved)
                        finally:
                            try: _TELEGRAM_SEM.release()
                            except Exception: pass
            except Exception:
                log.exception("complete_albums_tick: telegram fill failed for row=%d", cid)

            if moved:
                merged = list(imported_paths) + moved
                added_bytes = 0
                for p in moved:
                    try: added_bytes += _os.path.getsize(p)
                    except Exception: pass
                _stamp(commit_progress=(merged, added_bytes))
                result["completed_more"] += 1
                result["tracks_added"] += len(moved)
                log.info("complete_albums_tick: row=%d +%d tracks (now %d/%d)",
                         cid, len(moved), len(merged), total)
            else:
                _stamp()

            # Cap wasteful re-downloads: count consecutive zero-progress attempts. An album
            # whose remaining "missing" tracks are all songs we already have elsewhere will
            # never add anything — once the streak hits the cap it drops out of the candidate
            # pool (see selection above) instead of re-downloading forever.
            try:
                with SessionLocal() as _zs:
                    _zr = _zs.query(AutofillAction).filter(AutofillAction.id == cid).first()
                    if _zr is not None:
                        _zr.complete_zero_streak = (
                            0 if moved else int(getattr(_zr, "complete_zero_streak", 0) or 0) + 1
                        )
                        _zs.commit()
            except Exception:
                pass

            try:
                if _os.path.isdir(tmp_dir):
                    _shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass
    except Exception:
        log.exception("complete_albums_tick: unexpected failure")
    finally:
        try:
            _COMPLETE_ALBUMS_SEM.release()
        except Exception:
            pass
    log.info("complete_albums_tick: %s", result)
    return result


def _validate_positive_mappings_step(batch: int = 300) -> dict:
    """Mappings can go STALE-POSITIVE: tile merges, consolidations, and Plex
    dances retire ratingKeys, but nothing ever re-checked existing matches — a
    dead key stayed 'matched' forever, overstating the dashboard's Plex
    coverage and silently dropping the track from compiled Plex playlists.

    Walks the positive mappings in a rotating window (cursor in config) and
    batch-verifies the keys against Plex; dead ones get plex_track_key nulled
    and plex_searched_at cleared so rematch_plex_misses_tick re-maps them to
    their post-merge keys within minutes (the files are still on disk)."""
    out = {"validated": 0, "dead": 0}
    try:
        from .db import SessionLocal, TrackMapping
        from . import plex_client
    except Exception:
        return out
    srv = plex_client._connect()
    if not srv:
        return out
    try:
        cursor = int(get_config("mapping_validate_cursor", "0") or "0")
    except Exception:
        cursor = 0
    new_cursor = None
    with SessionLocal() as s_:
        rows = (s_.query(TrackMapping)
                .filter(TrackMapping.plex_track_key.isnot(None), TrackMapping.id > cursor)
                .order_by(TrackMapping.id).limit(batch).all())
        if not rows:
            set_config("mapping_validate_cursor", "0")  # wrap; next run starts over
            return out
        new_cursor = rows[-1].id
        ids = {}
        for r in rows:
            k = (r.plex_track_key or "").rsplit("/", 1)[-1]
            if k.isdigit():
                ids.setdefault(int(k), []).append(r)
        alive = set()
        idlist = sorted(ids)
        for i in range(0, len(idlist), 100):
            chunk = idlist[i:i + 100]
            try:
                for it in srv.fetchItems("/library/metadata/%s" % ",".join(map(str, chunk))):
                    alive.add(int(it.ratingKey))
            except Exception:
                # 404 means the whole chunk is gone — confirm one by one
                for one in chunk:
                    try:
                        srv.fetchItem(one)
                        alive.add(one)
                    except Exception:
                        pass
        out["validated"] = len(idlist)
        for k, rs in ids.items():
            if k in alive:
                continue
            for r in rs:
                r.plex_track_key = None
                r.plex_searched_at = None  # instantly eligible for rematch
                out["dead"] += 1
        s_.commit()
    if new_cursor is not None:
        set_config("mapping_validate_cursor", str(new_cursor))
    if out["dead"]:
        log.info("mapping-validate: %d/%d positive mappings dead — queued for rematch",
                 out["dead"], out["validated"])
    return out



def rematch_plex_misses_tick(batch: int = 80) -> dict:
    """Re-match tracks that are DOWNLOADED but still cached as a Plex 'miss'.

    The matcher keeps a 24h negative cache: once a Plex search misses, it won't
    re-search for PLEX_NEGATIVE_CACHE_TTL_HOURS. So a song downloaded AFTER its
    last miss-search keeps showing as 'not in Plex' (greyed out in the playlists
    tab) for up to a day — e.g. Quinn XCII 'HOOPLA'.

    This targets only tracks we KNOW are on disk (an AutofillAction with status
    imported/library_existing references the id) whose newest TrackMapping has no
    plex_track_key, re-searches Plex now, and writes the positive mapping the
    moment it's found — so the UI flips to 'downloaded' within minutes instead of
    a day. Only ever writes a *positive* mapping; once matched the track drops out
    of the miss set, so the work self-drains.

    A track we re-search and STILL can't find (genuinely not in Plex under that
    title/artist) gets its negative timestamp refreshed and is then skipped for
    REMATCH_NEG_TTL_HOURS, so we don't hammer Plex with the same hopeless search
    every tick. Holds NO DB session during the (network) Plex searches.
    """
    import json as _json
    from datetime import datetime as _dt, timedelta as _td
    REMATCH_NEG_TTL_HOURS = 1
    out = {"downloaded": 0, "misses": 0, "eligible": 0, "checked": 0, "fixed": 0}
    # Stale-positive guard: verify a rotating window of existing matches first,
    # so keys retired by merges/dances get nulled and re-mapped right here.
    try:
        out["mapping_validate"] = _validate_positive_mappings_step()
    except Exception:
        log.exception("rematch: mapping validation step failed (continuing)")
    try:
        from .db import SessionLocal, TrackMapping, AutofillAction, LocalTrack
        from .matcher import save_mapping, mark_plex_search_attempted
        from . import plex_client
        from sqlalchemy import select as _select
    except Exception:
        log.exception("rematch_plex_misses_tick: import failed")
        return out

    downloaded = set()
    meta = {}
    to_search = []
    with SessionLocal() as s:
        for aa in s.scalars(_select(AutofillAction).where(
                AutofillAction.status.in_(("imported", "library_existing")))).all():
            if not aa.track_ids_json:
                continue
            try:
                for tid in _json.loads(aa.track_ids_json):
                    if tid:
                        downloaded.add(tid)
            except Exception:
                pass
        out["downloaded"] = len(downloaded)
        if not downloaded:
            return out
        dl = list(downloaded)

        # newest mapping per id: capture both the plex key and the last search time
        mapinfo = {}
        for i in range(0, len(dl), 500):
            chunk = dl[i:i + 500]
            for tm in s.scalars(_select(TrackMapping).where(
                    TrackMapping.spotify_track_id.in_(chunk))).all():
                d = mapinfo.setdefault(tm.spotify_track_id, {"key": None, "searched": None})
                if tm.plex_track_key and not d["key"]:
                    d["key"] = tm.plex_track_key
                if tm.plex_searched_at and (d["searched"] is None or tm.plex_searched_at > d["searched"]):
                    d["searched"] = tm.plex_searched_at
        positive = {t for t, d in mapinfo.items() if d["key"]}
        miss = [t for t in dl if t not in positive]
        out["misses"] = len(miss)
        if not miss:
            return out
        cutoff = _dt.utcnow() - _td(hours=REMATCH_NEG_TTL_HOURS)
        eligible = [t for t in miss
                    if not (mapinfo.get(t, {}).get("searched") and mapinfo[t]["searched"] >= cutoff)]
        out["eligible"] = len(eligible)
        to_search = eligible[:max(1, batch)]
        if not to_search:
            return out

        # metadata to search with — LocalTrack (playlist mirror) first
        for i in range(0, len(to_search), 500):
            chunk = to_search[i:i + 500]
            for lt in s.scalars(_select(LocalTrack).where(
                    LocalTrack.spotify_track_id.in_(chunk))).all():
                if lt.spotify_track_id not in meta and (lt.title or lt.artist):
                    meta[lt.spotify_track_id] = (lt.title or "", lt.artist or "",
                                                 getattr(lt, "duration_ms", None))
        try:
            from .db import SpotifyLikedTrack
            need = [t for t in to_search if t not in meta]
            for i in range(0, len(need), 500):
                chunk = need[i:i + 500]
                for k in s.scalars(_select(SpotifyLikedTrack).where(
                        SpotifyLikedTrack.spotify_track_id.in_(chunk))).all():
                    if k.spotify_track_id not in meta:
                        meta[k.spotify_track_id] = (getattr(k, "title", "") or "",
                                            getattr(k, "artist", "") or "",
                                            getattr(k, "duration_ms", None))
        except Exception:
            pass

    # re-search Plex (no DB session held); persist positives, back off on misses
    fixed = checked = 0
    for tid in to_search:
        info = meta.get(tid)
        if not info or not (info[0] or info[1]):
            continue
        title, artist, dur = info
        try:
            found = plex_client.search_track(title, artist, dur)
        except Exception:
            found = None
        checked += 1
        if found and getattr(found, "key", None):
            try:
                save_mapping(spotify_id=tid, plex_key=found.key,
                             title=getattr(found, "title", "") or title,
                             artist=getattr(found, "artist", "") or artist,
                             method="plex/rematch", confidence=90)
                fixed += 1
            except Exception:
                log.exception("rematch_plex_misses_tick: save_mapping failed for %s", tid)
        else:
            # still not in Plex under this title/artist — refresh the negative
            # timestamp so we don't re-search this hopeless one every tick.
            try:
                mark_plex_search_attempted(tid)
            except Exception:
                pass
    out["checked"] = checked
    out["fixed"] = fixed
    if fixed or out["eligible"]:
        log.info("rematch_plex_misses_tick: %d downloaded, %d unmatched (%d eligible), %d checked, %d re-matched",
                 out["downloaded"], out["misses"], out["eligible"], checked, fixed)
    return out
