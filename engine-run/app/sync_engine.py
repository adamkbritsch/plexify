"""Spotify-as-canonical-source playlist sync.

Architecture (after the v2 refactor):
- Spotify is the ONLY source of truth. Pairs without a Spotify playlist are skipped.
- Two cadences:
    * Additions (fast, every 5 min): propagate newly-added Spotify tracks to destinations.
    * Full reconciliation (slow, hourly): same as additions PLUS propagate deletions.
- Destinations today: Tidal, Plex (FLAC-only). Architecture is generic; adding
  more destinations is a new client module + a propagation block.
- Resilience: if any destination's auth is dead, log a warning and continue with
  the rest; never fail a whole pair because one destination is down.
- Order preservation: snapshots are stored as ordered lists; additions to
  destinations are made in the Spotify playlist's order.
"""
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select

from . import auth_spotify, jobs, plex_client, spotify_client
from .db import (
    AppConfig, LocalTrack, PlaylistPair, PlaylistSnapshot, SyncRun, TrackMapping, UnmatchedTrack,
    get_config, session_scope,
)
from .matcher import lookup_cached, save_mapping
from .autofill_engine import LIKED_SONGS_SENTINEL

log = logging.getLogger(__name__)

_global_sync_lock = threading.Lock()
_pair_locks_guard = threading.Lock()
_pair_locks: dict[int, threading.Lock] = {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _emit(job_id: Optional[int], message: str, level: str = "info") -> None:
    if job_id is not None:
        try:
            jobs.emit(job_id, message, level=level)
        except Exception:
            log.exception("job emit failed")


def _plex_pending_create(pair_id: int) -> Optional[str]:
    return get_config(f"plex_pending_create_{pair_id}")


def _clear_plex_pending(pair_id: int) -> None:
    with session_scope() as s:
        s.query(AppConfig).filter(AppConfig.key == f"plex_pending_create_{pair_id}").delete(
            synchronize_session=False
        )


def _pair_lock(pair_id: int) -> threading.Lock:
    with _pair_locks_guard:
        lock = _pair_locks.get(pair_id)
        if lock is None:
            lock = threading.Lock()
            _pair_locks[pair_id] = lock
        return lock


def forget_pair_lock(pair_id: int) -> None:
    """Q14: call after delete_pair so the lock dict doesn't grow unbounded."""
    with _pair_locks_guard:
        _pair_locks.pop(pair_id, None)


def _load_snapshot(pair_id: int, service: str) -> Optional[list[str]]:
    with session_scope() as s:
        row = s.scalar(
            select(PlaylistSnapshot).where(
                PlaylistSnapshot.pair_id == pair_id,
                PlaylistSnapshot.service == service,
            )
        )
        if not row:
            return None
        # D-10: corrupt JSON shouldn't poison every subsequent sync — treat as fresh.
        try:
            data = json.loads(row.track_ids_json or "[]")
        except (json.JSONDecodeError, TypeError):
            log.warning("snapshot corrupt for pair=%d service=%s; treating as fresh", pair_id, service)
            return None
        return data if isinstance(data, list) else []


def _save_snapshot(pair_id: int, service: str, ids_in_order) -> None:
    payload = json.dumps(list(ids_in_order))
    with session_scope() as s:
        row = s.scalar(
            select(PlaylistSnapshot).where(
                PlaylistSnapshot.pair_id == pair_id,
                PlaylistSnapshot.service == service,
            )
        )
        if row:
            row.track_ids_json = payload
            row.taken_at = _utcnow()
        else:
            s.add(PlaylistSnapshot(pair_id=pair_id, service=service, track_ids_json=payload))


def _record_unmatched(pair_id: int, target: str, t, reason: str) -> None:
    with session_scope() as s:
        existing = s.scalar(
            select(UnmatchedTrack).where(
                UnmatchedTrack.pair_id == pair_id,
                UnmatchedTrack.source_service == "spotify",
                UnmatchedTrack.target_service == target,
                UnmatchedTrack.source_track_id == t.id,
            )
        )
        if existing:
            existing.last_seen_at = _utcnow()
            existing.reason = reason
        else:
            s.add(UnmatchedTrack(
                pair_id=pair_id, source_service="spotify", target_service=target,
                source_track_id=t.id,
                title=(t.title or "")[:500], artist=(t.artist or "")[:500],
                album=(t.album or "")[:500] if t.album else None,
                isrc=t.isrc, reason=reason,
            ))



def _resolve_to_plex(sp_track) -> Optional[str]:
    """Look up or search for a FLAC match in Plex. Uses negative cache.

    Bug #4 fix: skips Plex search if we tried recently and missed.
    Bug #5 fix: if cached key fails to validate (track deleted from Plex),
    invalidate and search fresh.
    """
    from .matcher import plex_search_was_recent, mark_plex_search_attempted
    cached = lookup_cached(spotify_id=sp_track.id)
    if cached and cached.plex_track_key:
        # Validate the cached key — it may have been deleted from Plex.
        # Only check periodically; the validate call itself is an API hit.
        if plex_client.validate_track(cached.plex_track_key):
            return cached.plex_track_key
        # Stale — fall through to re-search
        log.info("Plex track %s no longer exists; re-searching for spotify_id %s",
                 cached.plex_track_key, sp_track.id)
        # Clear the stale mapping by overwriting on next save_mapping
    elif plex_search_was_recent(cached):
        # Negative cache hit: we searched recently and missed; don't bother again
        return None

    found = plex_client.search_track(sp_track.title, sp_track.artist, sp_track.duration_ms)
    if not found:
        mark_plex_search_attempted(sp_track.id)
        return None
    save_mapping(spotify_id=sp_track.id, plex_key=found.key,
                 isrc=sp_track.isrc, title=found.title, artist=found.artist,
                 method=f"plex/{found.codec}", confidence=90)
    mark_plex_search_attempted(sp_track.id)  # also records a hit timestamp
    return found.key


def sync_pair(pair_id: int, *, include_deletions: bool = False, job_id: Optional[int] = None) -> dict:
    """Propagate a single Spotify playlist to its destinations.

    include_deletions=False is the fast path (additions only).
    include_deletions=True is the slow reconciliation pass (also removes from destinations).
    """
    summary = {
        "added_to_tidal": 0, "removed_from_tidal": 0, "tidal_unmatched": 0,
        "added_to_plex": 0, "removed_from_plex": 0, "plex_misses": 0,
        "status": "ok", "error": None,
    }
    lock = _pair_lock(pair_id)
    if not lock.acquire(blocking=False):
        summary["status"] = "skipped"
        summary["error"] = "another sync of this pair is in progress"
        _emit(job_id, f"pair {pair_id}: skipped (already running)", level="warning")
        return summary
    try:
        return _sync_pair_locked(pair_id, summary, include_deletions=include_deletions, job_id=job_id)
    finally:
        lock.release()


def _sync_pair_locked(pair_id: int, summary: dict, *, include_deletions: bool,
                       job_id: Optional[int]) -> dict:
    with session_scope() as s:
        pair = s.get(PlaylistPair, pair_id)
        if not pair or not pair.enabled:
            summary["status"] = "skipped"
            return summary
        sp_id = pair.spotify_playlist_id
        plex_key = pair.plex_playlist_key
        plex_enabled = bool(pair.plex_enabled)
        pair_name = pair.name

    # AUDIT-LIKED: sentinel pair is managed by sync_spotify_liked_tracks_tick,
    # not by Spotify's playlist API.
    if sp_id == LIKED_SONGS_SENTINEL:
        summary["status"] = "skipped"
        summary["error"] = "liked_songs_sentinel — managed by sync_spotify_liked_tracks_tick"
        return summary

    if not sp_id:
        summary["status"] = "skipped"
        summary["error"] = "no Spotify source (Spotify is the only allowed source)"
        return summary
    if not auth_spotify.is_authed():
        summary["status"] = "error"
        summary["error"] = "Spotify not authenticated"
        _emit(job_id, f"[{pair_name}] Spotify not authenticated", level="error")
        return summary

    run_id: Optional[int] = None
    with session_scope() as s:
        run = SyncRun(pair_id=pair_id)
        s.add(run)
        s.flush()
        run_id = run.id

    try:
        _emit(job_id, f"[{pair_name}] fetching Spotify playlist…")
        try:
            sp_tracks = spotify_client.get_playlist_tracks(sp_id)
        except spotify_client.PlaylistForbiddenError as e:
            summary["status"] = "error"
            summary["error"] = str(e)[:500]
            _emit(job_id, f"[{pair_name}] {e}", level="error")
            return summary
        sp_order = [t.id for t in sp_tracks]
        sp_set = {t.id for t in sp_tracks}
        sp_by_id = {t.id: t for t in sp_tracks}
        _emit(job_id, f"[{pair_name}] Spotify: {len(sp_tracks)} tracks")

        last_sp_list = _load_snapshot(pair_id, "spotify") or []
        last_sp = set(last_sp_list)
        first_sync_for_pair = not last_sp

        added_set = sp_set - last_sp
        removed_set = (last_sp - sp_set) if include_deletions else set()

        # MIRROR FIX: include tracks that are still UNMATCHED in Plex on every
        # cycle. Without this, tracks excluded from `added_set` (because they
        # appeared in a previous snapshot) but never linked to a Plex track
        # (plex_track_key IS NULL) stay invisible forever, even as Plexify
        # delivers their files. Re-attempting unmatched tracks every cycle is
        # cheap because matcher caches negative results via plex_searched_at.
        unmatched_set: set[str] = set()
        existing_set: set[str] = set()
        sp_set_list = list(sp_set)
        with session_scope() as _s:
            # Chunk the IN-clause to stay under SQLite's 999-variable limit
            for chunk_i in range(0, len(sp_set_list), 500):
                chunk = sp_set_list[chunk_i:chunk_i + 500]
                # Unmatched: TrackMapping rows with NULL plex_track_key
                for r in _s.execute(
                    select(TrackMapping.spotify_track_id)
                    .where(TrackMapping.spotify_track_id.in_(chunk))
                    .where(TrackMapping.plex_track_key.is_(None))
                ).all():
                    unmatched_set.add(r[0])
                # All existing TrackMapping rows for this chunk
                for r in _s.execute(
                    select(TrackMapping.spotify_track_id).where(TrackMapping.spotify_track_id.in_(chunk))
                ).all():
                    existing_set.add(r[0])
        # Also include spotify_ids that have NO TrackMapping row at all
        for sid in sp_set:
            if sid not in existing_set:
                unmatched_set.add(sid)

        if first_sync_for_pair:
            # Seed destinations with the entire Spotify playlist on first sync
            propagate_in_order = list(sp_order)
            _emit(job_id, f"[{pair_name}] first sync — seeding destinations with full playlist")
        else:
            # Propagate NEW tracks + retry UNMATCHED tracks
            combined = added_set | unmatched_set
            propagate_in_order = [tid for tid in sp_order if tid in combined]
            if propagate_in_order:
                _emit(job_id, f"[{pair_name}] {len(added_set)} new + {len(unmatched_set)} unmatched to (re-)propagate")
            if include_deletions and removed_set:
                _emit(job_id, f"[{pair_name}] {len(removed_set)} deletions to propagate (slow pass)")

        # === Tidal removed ===

        # === PLEX ===
        if plex_enabled:
            if not plex_client.is_authed():
                _emit(job_id, f"[{pair_name}] Plex disconnected — skipping Plex side", level="warning")
            else:
                plex_key = _propagate_to_plex(
                    pair_id=pair_id, pair_name=pair_name,
                    plex_playlist_key=plex_key,
                    sp_order=sp_order, sp_set=sp_set, sp_by_id=sp_by_id,
                    propagate_in_order=propagate_in_order,
                    removed_set=removed_set,
                    summary=summary, job_id=job_id,
                )

        # Save Spotify snapshot in source order (so we detect drift later)
        _save_snapshot(pair_id, "spotify", sp_order)

        # Record the current Spotify snapshot_id so the watcher can skip us next
        # tick. Must happen after successful sync — if we crash mid-sync, the
        # next tick will see the OLD snapshot_id and retry.
        try:
            snap = spotify_client.get_playlist_snapshot_id(sp_id)
            if snap:
                with session_scope() as s:
                    p = s.get(PlaylistPair, pair_id)
                    if p:
                        p.last_spotify_snapshot_id = snap
                        p.last_changed_at = _utcnow()
        except Exception as e:
            log.debug("could not persist snapshot_id for pair %d: %s", pair_id, e)

    except Exception as e:
        log.exception("[%s] sync failed: %s", pair_name, e)
        summary["status"] = "error"
        summary["error"] = str(e)[:500]
        _emit(job_id, f"[{pair_name}] sync failed: {e}", level="error")

    with session_scope() as s:
        run = s.get(SyncRun, run_id)
        if run:
            run.finished_at = _utcnow()
            run.status = summary["status"]
            run.added_to_spotify = 0  # legacy column, always 0 in new model
            run.added_to_tidal = summary["added_to_tidal"]
            run.removed_from_tidal = summary["removed_from_tidal"]
            run.added_to_plex = summary["added_to_plex"]
            run.removed_from_plex = summary["removed_from_plex"]
            run.unmatched = summary["tidal_unmatched"]
            run.plex_misses = summary["plex_misses"]
            run.error_message = summary["error"]
    return summary



def _propagate_to_plex(*, pair_id: int, pair_name: str, plex_playlist_key: Optional[str],
                       sp_order: list, sp_set: set, sp_by_id: dict,
                       propagate_in_order: list[str], removed_set: set,
                       summary: dict, job_id: Optional[int]) -> Optional[str]:
    # Create Plex playlist on demand (with seed in source order)
    if not plex_playlist_key:
        pending_name = _plex_pending_create(pair_id) or pair_name
        seed: list[str] = []
        for sp_id in sp_order:
            if sp_id not in sp_set:
                continue
            sp_t = sp_by_id.get(sp_id)
            if not sp_t:
                continue
            pkey = _resolve_to_plex(sp_t)
            if pkey:
                seed.append(pkey)
                break
        if not seed:
            _emit(job_id, f"[{pair_name}] no FLAC matches yet in Plex — leaving Plex playlist uncreated",
                  level="warning")
            return None
        new_key = plex_client.create_playlist(pending_name, seed)
        if not new_key:
            _emit(job_id, f"[{pair_name}] Plex playlist creation failed", level="error")
            return None
        plex_playlist_key = new_key
        _clear_plex_pending(pair_id)
        with session_scope() as s:
            pair = s.get(PlaylistPair, pair_id)
            if pair:
                pair.plex_playlist_key = new_key
        summary["added_to_plex"] += len(seed)
        _emit(job_id, f"[{pair_name}] created Plex playlist {new_key}")

    # Bug #6 fix: handle stale plex_playlist_key (playlist was deleted in Plex).
    try:
        plex_tracks = plex_client.get_playlist_tracks(plex_playlist_key)
    except (plex_client.NotFound, AttributeError):
        # L-3: only "playlist gone" is a recoverable error here. Other failures
        # (network, auth, 5xx) MUST bubble up so we don't wipe plex_playlist_key
        # and create a duplicate playlist on retry.
        log.warning("[%s] Plex playlist %s no longer exists; recreating", pair_name, plex_playlist_key)
        _emit(job_id, f"[{pair_name}] Plex playlist {plex_playlist_key} disappeared; re-creating", level="warning")
        with session_scope() as s:
            p = s.get(PlaylistPair, pair_id)
            if p:
                p.plex_playlist_key = None
        plex_playlist_key = None
        # Recurse via the create-on-demand path at top of this function
        return _propagate_to_plex(
            pair_id=pair_id, pair_name=pair_name, plex_playlist_key=None,
            sp_order=sp_order, sp_set=sp_set, sp_by_id=sp_by_id,
            propagate_in_order=propagate_in_order, removed_set=removed_set,
            summary=summary, job_id=job_id,
        )
    plex_set = {t.key for t in plex_tracks}

    # Same auto-readd policy as Tidal: iterate ALL local tracks, use cache for fast skips,
    # only do live search for newly-added tracks.
    new_set = set(propagate_in_order)
    all_local_ids = list(sp_by_id.keys())

    cached_by_sp: dict[str, str] = {}
    if all_local_ids:
        with session_scope() as s:
            for chunk_start in range(0, len(all_local_ids), 500):
                chunk = all_local_ids[chunk_start:chunk_start + 500]
                rows = s.scalars(
                    select(TrackMapping).where(
                        TrackMapping.spotify_track_id.in_(chunk),
                        TrackMapping.plex_track_key.isnot(None),
                    )
                ).all()
                for r in rows:
                    cached_by_sp[r.spotify_track_id] = r.plex_track_key

    if new_set:
        _emit(job_id, f"[{pair_name}] {len(new_set)} new tracks to match; scanning {len(all_local_ids)} local tracks for missing Plex entries…")
    else:
        _emit(job_id, f"[{pair_name}] scanning {len(all_local_ids)} local tracks for missing Plex entries…")

    keys_to_add: list[str] = []
    miss_count = 0
    readded_count = 0
    for i, sp_track_id in enumerate(all_local_ids, 1):
        sp_t = sp_by_id.get(sp_track_id)
        if not sp_t:
            continue
        pkey = cached_by_sp.get(sp_track_id)
        if not pkey and sp_track_id in new_set:
            if job_id and (i % 5 == 0 or i == len(all_local_ids)):
                jobs.progress(job_id, current=i, total=len(all_local_ids),
                              step=f"Plex search {i}/{len(all_local_ids)}: {sp_t.title[:50]} — {sp_t.artist[:40]}")
            pkey = _resolve_to_plex(sp_t)
            if not pkey:
                miss_count += 1
                _record_unmatched(pair_id, "plex", sp_t, "no lossless copy in Plex (needs Lidarr)")
                continue
        elif not pkey:
            continue
        if pkey in plex_set or pkey in keys_to_add:
            continue
        if sp_track_id not in new_set:
            readded_count += 1
        keys_to_add.append(pkey)
    if readded_count:
        _emit(job_id, f"[{pair_name}] re-adding {readded_count} tracks that were manually removed from Plex")

    summary["plex_misses"] = miss_count

    if keys_to_add:
        CHUNK = 100
        # Maintain an ordered list of Plex keys so the snapshot reflects real order.
        plex_order = [t.key for t in plex_tracks]
        for ci in range(0, len(keys_to_add), CHUNK):
            chunk = keys_to_add[ci:ci + CHUNK]
            _emit(job_id, f"[{pair_name}] Plex add: chunk {ci // CHUNK + 1} ({ci + 1}-{ci + len(chunk)} of {len(keys_to_add)})")
            added = plex_client.add_tracks(plex_playlist_key, chunk)
            summary["added_to_plex"] += len(added)
            for k in added:
                if k not in plex_set:
                    plex_order.append(k)
                    plex_set.add(k)
            # Persist progress immediately so a crash next loop doesn't redo this chunk
            _save_snapshot(pair_id, "plex", plex_order)
            if job_id:
                jobs.progress(job_id,
                              step=f"[{pair_name}] Plex added {ci + len(chunk)}/{len(keys_to_add)}")
            if not added:
                _emit(job_id, f"[{pair_name}] Plex add chunk returned 0 — likely API failure; stopping", level="warning")
                break

    if removed_set:
        keys_to_remove: list[str] = []
        for sp_id in removed_set:
            cached = lookup_cached(spotify_id=sp_id)
            if cached and cached.plex_track_key and cached.plex_track_key in plex_set:
                keys_to_remove.append(cached.plex_track_key)
        if keys_to_remove:
            _emit(job_id, f"[{pair_name}] removing {len(keys_to_remove)} tracks from Plex…")
            removed = plex_client.remove_tracks(plex_playlist_key, keys_to_remove)
            summary["removed_from_plex"] = len(removed)
            plex_set.difference_update(removed)

    current = plex_client.get_playlist_tracks(plex_playlist_key)
    _save_snapshot(pair_id, "plex", [t.key for t in current])
    return plex_playlist_key


def sync_all(*, include_deletions: bool = False, job_id: Optional[int] = None) -> list[dict]:
    """Sync every enabled pair. `include_deletions=True` is the slow reconciliation pass."""
    if not _global_sync_lock.acquire(blocking=False):
        log.info("sync_all: another full sync is already running")
        _emit(job_id, "another full sync is already running — skipped", level="warning")
        if job_id:
            jobs.fail(job_id, "another full sync is already running")
        return []
    try:
        with session_scope() as s:
            rows = list(s.scalars(
                select(PlaylistPair).where(
                    PlaylistPair.enabled == True,
                    PlaylistPair.spotify_playlist_id.isnot(None),
                )
            ).all())
            pair_info = [(p.id, p.name) for p in rows]

        if job_id:
            jobs.start(job_id)
            jobs.progress(job_id, current=0, total=len(pair_info),
                          step=f"0/{len(pair_info)} pairs")
            mode = "with deletions" if include_deletions else "additions only"
            _emit(job_id, f"sync_all ({mode}): {len(pair_info)} pairs to process")

        results = []
        for i, (pid, pname) in enumerate(pair_info, 1):
            if job_id:
                jobs.progress(job_id, current=i - 1, step=f"{i}/{len(pair_info)} — {pname}")
            _emit(job_id, f"=== pair {i}/{len(pair_info)}: {pname} ===")
            results.append({"pair_id": pid, **sync_pair(pid, include_deletions=include_deletions, job_id=job_id)})

        if job_id:
            totals = {"added_to_tidal": 0, "removed_from_tidal": 0,
                      "added_to_plex": 0, "removed_from_plex": 0,
                      "plex_misses": 0, "tidal_unmatched": 0}
            errors = 0
            for r in results:
                if r.get("status") == "error":
                    errors += 1
                for k in totals:
                    totals[k] += r.get(k, 0)
            msg = (f"done: {len(results)} pairs"
                   f" · +{totals['added_to_tidal']} Tidal"
                   f" · +{totals['added_to_plex']} Plex"
                   f" · -{totals['removed_from_tidal']} Tidal -{totals['removed_from_plex']} Plex"
                   f" · {totals['plex_misses']} Plex misses · errors {errors}")
            jobs.finish(job_id, status="ok" if errors == 0 else "partial",
                        result={"totals": totals, "errors": errors, "results": results},
                        message=msg)
        return results
    except Exception as e:
        log.exception("sync_all failed: %s", e)
        if job_id:
            jobs.fail(job_id, str(e))
        raise
    finally:
        _global_sync_lock.release()






# ====================================================================
# COMPILE — incremental, resumable Spotify→local-backup ingest.
#
# Vision: Spotify is touched ONLY here. Every other operation (Tidal mirror,
# Plex mirror, UI rendering) reads from local_tracks. If the NAS restarts
# mid-compile, the next watcher tick resumes from PlaylistPair.compile_offset.
# ====================================================================
class _LocalSpTrack:
    """Adapter so propagation helpers can iterate local_tracks like Spotify tracks."""
    __slots__ = ("id", "title", "artist", "album", "isrc", "duration_ms")
    def __init__(self, row: LocalTrack):
        self.id = row.spotify_track_id
        self.title = row.title or ""
        self.artist = row.artist or ""
        self.album = row.album
        self.isrc = row.isrc
        self.duration_ms = row.duration_ms or 0


def compile_playlist(pair_id: int, *, job_id: Optional[int] = None,
                     known_snapshot_id: Optional[str] = None,
                     fresh: bool = False) -> dict:
    """Incrementally pull a Spotify playlist into local_tracks. Crash-resumable.

    fresh=True wipes compile_offset to 0 (used when watcher detects snapshot_id change).
    known_snapshot_id, when provided by the caller (typically the watcher), is persisted
    on success — avoids a separate API call to re-fetch the snapshot.
    """
    summary = {
        "status": "ok", "tracks_added": 0, "tracks_removed": 0,
        "starting_offset": 0, "final_offset": 0, "total": 0, "error": None,
    }
    lock = _pair_lock(pair_id)
    if not lock.acquire(blocking=False):
        summary["status"] = "skipped"
        summary["error"] = "another op is in progress for this pair"
        return summary
    try:
        with session_scope() as s:
            pair = s.get(PlaylistPair, pair_id)
            if not pair or not pair.spotify_playlist_id:
                summary["status"] = "skipped"
                return summary
            sp_id = pair.spotify_playlist_id
            pair_name = pair.name
            # AUDIT-LIKED: sentinel pair has no Spotify playlist to compile from.
            if sp_id == LIKED_SONGS_SENTINEL:
                log.debug("compile_playlist: skipping sentinel LikedSongs (LocalTracks managed elsewhere)")
                summary["status"] = "skipped"
                summary["error"] = "liked_songs_sentinel"
                return summary
            if fresh:
                pair.compile_offset = 0
                pair.compile_started_at = _utcnow()
                pair.compile_completed_at = None
            offset = int(pair.compile_offset or 0)
            pair.compile_status = "compiling"
            if not pair.compile_started_at:
                pair.compile_started_at = _utcnow()
        summary["starting_offset"] = offset
        _emit(job_id, f"[{pair_name}] compile: starting at offset {offset}{' (fresh)' if fresh else ''}")

        # Track every Spotify track ID we see this compile. Used at the end to
        # delete any local_tracks no longer present on Spotify.
        seen_ids: set[str] = set()
        first_page_total = None  # use first page's total as authoritative
        # If we're resuming, the IDs we already saved aren't in this set yet —
        # don't delete them as "unseen". Pre-load the IDs above the resume offset.
        if offset > 0:
            with session_scope() as s:
                resumed = list(s.scalars(
                    select(LocalTrack.spotify_track_id).where(LocalTrack.pair_id == pair_id)
                ).all())
                seen_ids.update(resumed)

        from sqlalchemy.exc import IntegrityError

        while True:
            def _on_retry_wait(secs, attempt, reason):
                _emit(job_id, f"[{pair_name}] waiting {secs}s on Spotify ({reason}, attempt {attempt+1})",
                      level="warning")

            try:
                positioned, raw_count, total, has_next = spotify_client.fetch_playlist_page(
                    sp_id, offset=offset, limit=100, patient=True, on_wait=_on_retry_wait,
                )
            except spotify_client.PlaylistForbiddenError as e:
                summary["status"] = "forbidden"
                summary["error"] = str(e)[:300]
                _emit(job_id, f"[{pair_name}] forbidden: {e}", level="error")
                with session_scope() as s:
                    p = s.get(PlaylistPair, pair_id)
                    if p: p.compile_status = "forbidden"
                return summary
            except spotify_client.PlaylistNotFoundError as e:
                summary["status"] = "unavailable"
                summary["error"] = str(e)[:300]
                _emit(job_id, f"[{pair_name}] playlist not found on Spotify: {e}", level="error")
                with session_scope() as s:
                    p = s.get(PlaylistPair, pair_id)
                    if p: p.compile_status = "unavailable"
                return summary
            except Exception as e:
                summary["status"] = "paused"
                summary["error"] = str(e)[:300]
                _emit(job_id, f"[{pair_name}] compile paused at offset {offset}: {e}",
                      level="warning")
                return summary

            if first_page_total is None:
                first_page_total = total  # lock in first read for consistency

            # If page came back empty AND we have no more pages, we're done.
            # If page is empty but has_next is True (shouldn't happen but be defensive),
            # advance by the requested limit so we don't loop. Q22: use the same
            # limit we passed to fetch_playlist_page (currently 100, but constant'd).
            PAGE_LIMIT = 100
            if raw_count == 0:
                if has_next:
                    offset += PAGE_LIMIT
                    continue
                break

            # Persist tracks at their ABSOLUTE positions (from fetch_playlist_page)
            with session_scope() as s:
                # Re-check pair still exists (could be deleted concurrently)
                p = s.get(PlaylistPair, pair_id)
                if not p:
                    summary["status"] = "cancelled"
                    _emit(job_id, f"[{pair_name}] pair deleted mid-compile; aborting", level="warning")
                    return summary
                for abs_position, t in positioned:
                    seen_ids.add(t.id)
                    existing = s.scalar(
                        select(LocalTrack).where(
                            LocalTrack.pair_id == pair_id,
                            LocalTrack.spotify_track_id == t.id,
                        )
                    )
                    if existing:
                        existing.position = abs_position
                        existing.title = (t.title or "")[:500]
                        existing.artist = (t.artist or "")[:500]
                        existing.album = (t.album or "")[:500] if t.album else None
                        existing.isrc = t.isrc
                        existing.duration_ms = t.duration_ms
                    else:
                        try:
                            s.add(LocalTrack(
                                pair_id=pair_id, position=abs_position,
                                spotify_track_id=t.id,
                                title=(t.title or "")[:500],
                                artist=(t.artist or "")[:500],
                                album=(t.album or "")[:500] if t.album else None,
                                isrc=t.isrc, duration_ms=t.duration_ms,
                            ))
                            s.flush()  # surface integrity errors per-row
                            summary["tracks_added"] += 1
                        except IntegrityError:
                            # Duplicate track in playlist — Spotify allows it.
                            # We already have it at an earlier position; keep that.
                            s.rollback()
                # Advance bookkeeping
                p.compile_offset = offset + raw_count
                # Use first_page_total (Spotify is more consistent that way) but cap at observed
                p.compile_total = first_page_total

            offset += raw_count
            summary["final_offset"] = offset
            summary["total"] = first_page_total

            if job_id:
                try:
                    jobs.progress(job_id, current=offset, total=first_page_total or offset,
                                  step=f"[{pair_name}] compile {offset}/{first_page_total} tracks")
                except Exception:
                    pass

            if not has_next:
                break

        # Compile is done. Two cleanup steps:
        # 1) Delete local_tracks for THIS pair whose spotify_track_id wasn't seen
        #    this round — those were removed from the Spotify playlist.
        # 2) Persist the snapshot_id (use known_snapshot_id from caller if available,
        #    else patient-fetch).
        # DB-LOCK FIX (#4): fetch the Spotify snapshot_id (network call that RETRIES via
        # patient=True) BEFORE opening the write transaction. Doing it inside held the
        # SQLite write lock across a slow network call — the same WAL-pin / write-lock-hold
        # class as the picker, and it fires on every compile while songs are streaming in.
        _compile_snap = known_snapshot_id or spotify_client.get_playlist_snapshot_id(sp_id, patient=True)
        with session_scope() as s:
            p = s.get(PlaylistPair, pair_id)
            if not p:
                return summary
            # D-5: empty-guard + chunked NOT-IN (SQLite has a 999-variable limit).
            if not seen_ids:
                # Empty seen_ids could be a fetch failure, not "playlist empty".
                # Skip cleanup rather than wiping every LocalTrack for the pair.
                removed_count = 0
            else:
                seen_list = list(seen_ids)
                survivors = set()
                CHUNK = 500
                for i in range(0, len(seen_list), CHUNK):
                    chunk = seen_list[i:i+CHUNK]
                    ids_in_chunk = {
                        t.id for t in s.query(LocalTrack.id).filter(
                            LocalTrack.pair_id == pair_id,
                            LocalTrack.spotify_track_id.in_(chunk),
                        ).all()
                    }
                    survivors.update(ids_in_chunk)
                all_ids = [t.id for t in s.query(LocalTrack.id).filter(LocalTrack.pair_id == pair_id).all()]
                stale_ids = [i for i in all_ids if i not in survivors]
                removed_count = 0
                for i in range(0, len(stale_ids), CHUNK):
                    chunk = stale_ids[i:i+CHUNK]
                    removed_count += s.query(LocalTrack).filter(LocalTrack.id.in_(chunk)).delete(synchronize_session=False)
            summary["tracks_removed"] = removed_count or 0
            p.compile_status = "complete"
            p.compile_completed_at = _utcnow()
            # compile_offset reflects what we processed; cap at total to keep UI sensible
            p.compile_offset = min(offset, first_page_total or offset)
            snap = _compile_snap
            if snap:
                p.last_spotify_snapshot_id = snap
                p.last_changed_at = _utcnow()
        _emit(job_id, f"[{pair_name}] compile COMPLETE: {offset} tracks "
                      f"(added {summary['tracks_added']}, removed {summary['tracks_removed']})")
    finally:
        lock.release()
    return summary




# ====================================================================
# MIRROR — Local backup → destinations. NEVER touches Spotify.
# ====================================================================
# AUDIT-ORDER: mirror preserves spotify track order in NEW additions because
# propagate_in_order is sorted by LocalTrack.position. Existing-vs-new boundary
# means a Plex playlist that already has tracks won't perfectly match spotify
# order until full-replace is implemented.


def upload_playlist_cover_from_spotify(pair_id: int) -> bool:
    """Fetch the Spotify playlist's cover image and upload to its Plex playlist.
    Returns True if uploaded, False if skipped/failed. Best-effort.
    Skips the sentinel '__LIKED_SONGS__' pair (Saved Tracks has no playlist image)."""
    from . import auth_spotify, spotify_client, plex_client
    from .db import PlaylistPair
    from .autofill_engine import LIKED_SONGS_SENTINEL
    import requests, tempfile, os
    with session_scope() as s:
        pair = s.get(PlaylistPair, pair_id)
        if not pair:
            return False
        sp_id = pair.spotify_playlist_id
        plex_key = pair.plex_playlist_key
        pair_name = pair.name
    if not plex_key or not sp_id:
        return False
    if sp_id == LIKED_SONGS_SENTINEL:
        return False  # Saved Tracks has no playlist image
    try:
        sp = auth_spotify.get_client()
        if not sp:
            return False
        # Fetch playlist metadata; image is in 'images'[0]['url'] (largest first)
        pl_meta = spotify_client._retry_patient(sp.playlist, sp_id, fields="images,name")
        if not pl_meta:
            return False
        images = pl_meta.get("images") or []
        if not images:
            log.debug("playlist cover: %s has no images", pair_name)
            return False
        img_url = images[0].get("url")
        if not img_url:
            return False
        # Download image to a temp file then upload via plexapi
        resp = requests.get(img_url, timeout=10)
        if resp.status_code != 200:
            log.warning("playlist cover: download HTTP %d for %s", resp.status_code, pair_name)
            return False
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
            tmp.write(resp.content)
            tmp_path = tmp.name
        try:
            srv = plex_client._connect()
            if not srv:
                return False
            plex_pl = srv.fetchItem(int(plex_key))
            if plex_pl is None:
                return False
            plex_pl.uploadPoster(filepath=tmp_path)
            log.info("playlist cover: uploaded %d bytes to Plex playlist %r",
                     len(resp.content), pair_name)
            return True
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception:
        log.exception("playlist cover: upload failed for pair_id=%d", pair_id)
        return False


def mirror_from_local(pair_id: int, *, include_deletions: bool = False,
                      job_id: Optional[int] = None) -> dict:
    """Propagate the local_tracks backup of a Spotify playlist to its destinations.

    NO Spotify API calls. Reads local_tracks, resolves to Tidal/Plex via the
    matcher cache (with live search as a cache-miss fallback), and writes to
    destination playlists in local order.
    """
    summary = {
        "added_to_tidal": 0, "removed_from_tidal": 0, "tidal_unmatched": 0,
        "added_to_plex": 0, "removed_from_plex": 0, "plex_misses": 0,
        "status": "ok", "error": None,
    }
    lock = _pair_lock(pair_id)
    if not lock.acquire(blocking=False):
        summary["status"] = "skipped"
        summary["error"] = "another op is in progress for this pair"
        return summary
    try:
        with session_scope() as s:
            pair = s.get(PlaylistPair, pair_id)
            if not pair or not pair.enabled:
                summary["status"] = "skipped"
                return summary
            plex_key = pair.plex_playlist_key
            plex_enabled = bool(pair.plex_enabled)
            pair_name = pair.name
            local_rows = list(s.scalars(
                select(LocalTrack).where(LocalTrack.pair_id == pair_id)
                .order_by(LocalTrack.position)
            ).all())

        if not local_rows:
            _emit(job_id, f"[{pair_name}] mirror: no local tracks yet (compile not done)")
            summary["status"] = "skipped"
            summary["error"] = "local backup is empty"
            return summary

        sp_tracks = [_LocalSpTrack(r) for r in local_rows]
        sp_order = [t.id for t in sp_tracks]
        sp_set = {t.id for t in sp_tracks}
        sp_by_id = {t.id: t for t in sp_tracks}
        _emit(job_id, f"[{pair_name}] mirror: {len(sp_tracks)} tracks in local backup")

        last_sp_list = _load_snapshot(pair_id, "spotify") or []
        last_sp = set(last_sp_list)
        first_mirror = not last_sp

        # MIRROR FIX: include unmatched tracks every cycle. Without this, tracks
        # excluded from added_set but never linked to a Plex track stay invisible.
        unmatched_set = set()
        existing_set = set()
        sp_set_list = list(sp_set)
        with session_scope() as _s:
            for ci in range(0, len(sp_set_list), 500):
                chunk = sp_set_list[ci:ci + 500]
                for r in _s.execute(select(TrackMapping.spotify_track_id).where(TrackMapping.spotify_track_id.in_(chunk)).where(TrackMapping.plex_track_key.is_(None))).all():
                    unmatched_set.add(r[0])
                for r in _s.execute(select(TrackMapping.spotify_track_id).where(TrackMapping.spotify_track_id.in_(chunk))).all():
                    existing_set.add(r[0])
        for sid in sp_set:
            if sid not in existing_set:
                unmatched_set.add(sid)

        if first_mirror:
            propagate_in_order = list(sp_order)
        else:
            added_set = sp_set - last_sp
            combined = added_set | unmatched_set
            propagate_in_order = [tid for tid in sp_order if tid in combined]
            _emit(job_id, f'[{pair_name}] mirror: {len(added_set)} new + {len(unmatched_set)} unmatched to (re-)propagate')
        removed_set = set() if (first_mirror or not include_deletions) else (last_sp - sp_set)


        if plex_enabled and plex_client.is_authed():
            _propagate_to_plex(
                pair_id=pair_id, pair_name=pair_name, plex_playlist_key=plex_key,
                sp_order=sp_order, sp_set=sp_set, sp_by_id=sp_by_id,
                propagate_in_order=propagate_in_order, removed_set=removed_set,
                summary=summary, job_id=job_id,
            )

                # Best-effort: upload Spotify playlist cover to Plex
        try:
            if summary.get('status') == 'ok':
                upload_playlist_cover_from_spotify(pair_id)
        except Exception:
            log.exception('cover upload raised (continuing)')
        _save_snapshot(pair_id, "spotify", sp_order)

    except Exception as e:
        log.exception("[%s] mirror failed: %s", pair_name, e)
        summary["status"] = "error"
        summary["error"] = str(e)[:500]
    finally:
        lock.release()
    return summary


# ====================================================================
# WATCHER — the tiny "what changed?" script.
#
# Instead of fetching every track in every pair every interval, this:
#   1. Makes ONE Spotify call to /me/playlists to get snapshot_id per playlist
#   2. Compares each pair's stored last_spotify_snapshot_id to the current one
#   3. Skips pairs where snapshot_id didn't change (Spotify guarantees the
#      playlist content is unchanged in that case)
#   4. Only fetches+diffs the small subset of playlists that actually changed
#
# Result: a 6-pair setup with no changes = 1 lightweight API call. Adding 1
# song to a playlist = 1 metadata call + 1 track fetch + 1 propagation per
# destination, scoped to just that one playlist.
# ====================================================================
def watch_for_changes(*, include_deletions: bool = False) -> dict:
    """The tiny watcher — fires on the scheduler.

    Three responsibilities, all cheap:
      1. Cheap snapshot_id check via /me/playlists (1 API call).
      2. Identify pairs that need work: incomplete compile, OR snapshot changed.
      3. Spawn ONE background job to process them sequentially.

    The scheduled tick returns in well under a second. Real work happens in
    the background thread which can run for hours without blocking.
    """
    summary = {"checked": 0, "needs_compile": 0, "needs_refresh": 0,
               "needs_mirror_only": 0, "spawned_job": None, "errors": 0}

    if not auth_spotify.is_authed():
        log.debug("watcher: Spotify not authed; skipping")
        return summary

    # Find pairs that need work BEFORE we make any Spotify call. This handles
    # the crash-resumed case where a pair was mid-compile when the NAS died:
    # the watcher just picks it back up, no metadata check needed.
    with session_scope() as s:
        pending_pairs = list(s.scalars(
            select(PlaylistPair).where(
                PlaylistPair.enabled == True,
                PlaylistPair.spotify_playlist_id.isnot(None),
                PlaylistPair.compile_status.in_(["pending", "compiling"]),
            )
        ).all())
        pending_compiles = [(p.id, p.name, p.spotify_playlist_id, p.compile_status) for p in pending_pairs]

    # Step 1: cheap metadata check — find playlists whose contents changed.
    try:
        spotify_playlists = spotify_client.list_my_playlists(use_cache=False, patient=False)
    except Exception as e:
        log.warning("watcher: list_my_playlists failed: %s", e)
        # Still process pending compiles even if metadata check fails — they're
        # crash-resumed work that doesn't need fresh snapshot_ids.
        spotify_playlists = []
        summary["errors"] += 1
    current_snapshots = {p.id: p.snapshot_id for p in spotify_playlists}
    summary["checked"] = len(current_snapshots)

    # Step 1b: AUTO-DISCOVER — create a PlaylistPair for any Spotify playlist we
    # haven't seen yet. The watcher does this on every tick so newly-created
    # Spotify playlists start syncing automatically. Pairs are created with
    # compile_status='pending' and NO destinations enabled — destinations are
    # opt-in per-pair via the dashboard toggles.
    summary["discovered_new"] = 0
    if spotify_playlists:
        with session_scope() as s:
            known_sp_ids = {
                p.spotify_playlist_id
                for p in s.scalars(
                    select(PlaylistPair).where(PlaylistPair.spotify_playlist_id.isnot(None))
                ).all()
            }
            from sqlalchemy.exc import IntegrityError as _IntErr
            for sp in spotify_playlists:
                if sp.id in known_sp_ids:
                    continue
                # Followed (not-owned) playlists: Spotify blocks third-party apps
                # from reading their items (403, 2025 dev-mode restriction) —
                # auto-creating a pair would just churn 'forbidden' forever.
                if not getattr(sp, "owned", True):
                    continue
                sp_id = sp.id
                sp_name = sp.name
                try:
                    # D-9: nested savepoint so a concurrent racer (e.g. another
                    # watcher tick) doesn't kill the whole batch on unique-conflict.
                    with s.begin_nested():
                        s.add(PlaylistPair(
                            name=sp_name,
                            spotify_playlist_id=sp_id,
                            enabled=True,
                            compile_status="pending",
                        ))
                    summary["discovered_new"] += 1
                    known_sp_ids.add(sp_id)
                    log.info("auto-discover: new pair for %r (sp=%s)", sp_name, sp_id)
                except _IntErr:
                    log.info("auto-discover: pair for sp=%s already exists (race), skipping", sp_id)

    # Step 2: pairs needing refresh (compile_status='complete' but snapshot_id changed).
    pairs_to_refresh: list[tuple[int, str, str, str]] = []
    with session_scope() as s:
        complete_pairs = list(s.scalars(
            select(PlaylistPair).where(
                PlaylistPair.enabled == True,
                PlaylistPair.spotify_playlist_id.isnot(None),
                PlaylistPair.compile_status == "complete",
            )
        ).all())
        for p in complete_pairs:
            current_snap = current_snapshots.get(p.spotify_playlist_id)
            if not current_snap:
                continue
            if current_snap != p.last_spotify_snapshot_id:
                pairs_to_refresh.append((p.id, p.name, p.spotify_playlist_id, current_snap))
            else:
                # No Spotify change — but mirrors might still need work (e.g., a
                # destination service was disconnected during last sync).
                # Most ticks: this is a no-op for an already-mirrored pair.
                pass

    summary["needs_compile"] = len(pending_compiles)
    summary["needs_refresh"] = len(pairs_to_refresh)

    if not pending_compiles and not pairs_to_refresh:
        log.info("watcher: nothing to do (%d compiled, %d scanned)",
                 len(complete_pairs), summary["checked"])
        return summary

    # Step 3: spawn one background worker.
    total_units = len(pending_compiles) + len(pairs_to_refresh)
    title = (f"Reconcile {total_units} playlist(s)" if include_deletions
             else f"Sync {total_units} playlist(s)")
    job_id = jobs.create("watcher_sync", title=title, total=total_units)
    summary["spawned_job"] = job_id

    def runner():
        jobs.start(job_id, step=f"{total_units} playlist(s) to process")
        unit = 0
        errors = 0

        # Pending compiles first (these resume from compile_offset)
        for pid, pname, sp_id, status in pending_compiles:
            unit += 1
            jobs.progress(job_id, current=unit - 1,
                          step=f"{unit}/{total_units}: compile {pname} ({status})")
            _emit(job_id, f"=== compile: {pname} ({status}) ===")
            try:
                r = compile_playlist(pid, job_id=job_id)
                if r["status"] == "ok":
                    _emit(job_id, f"  compiled {r['final_offset']} tracks ({r['tracks_added']} new)")
                    # Now mirror it
                    mirror_from_local(pid, include_deletions=include_deletions, job_id=job_id)
                elif r["status"] == "paused":
                    _emit(job_id, f"  paused at offset {r['final_offset']} — will resume next tick")
                else:
                    errors += 1
            except Exception:
                log.exception("compile_playlist(%d) failed", pid)
                errors += 1

        # Then refresh-and-mirror for pairs whose Spotify contents changed
        for pid, pname, sp_id, new_snap in pairs_to_refresh:
            unit += 1
            jobs.progress(job_id, current=unit - 1,
                          step=f"{unit}/{total_units}: refresh {pname}")
            _emit(job_id, f"=== refresh: {pname} (snapshot changed) ===")
            try:
                # fresh=True wipes compile_offset; known_snapshot_id avoids a
                # separate API call at the end of compile.
                r = compile_playlist(pid, job_id=job_id,
                                     known_snapshot_id=new_snap, fresh=True)
                if r["status"] == "ok":
                    mirror_from_local(pid, include_deletions=include_deletions, job_id=job_id)
                elif r["status"] != "paused":
                    errors += 1
            except Exception:
                log.exception("refresh(%d) failed", pid)
                errors += 1

        jobs.progress(job_id, current=total_units)
        jobs.finish(
            job_id,
            status="ok" if errors == 0 else "partial",
            message=f"{unit - errors} ok, {errors} errors",
        )

    threading.Thread(target=runner, daemon=True).start()
    return summary


def watch_additions() -> None:
    """Scheduled tiny watcher (every 5 min). Detects + dispatches; never blocks."""
    watch_for_changes(include_deletions=False)


def watch_with_deletions() -> None:
    """Scheduled slow watcher (every 60 min). Same detection, also propagates deletions."""
    watch_for_changes(include_deletions=True)


def trigger_immediate_watcher() -> None:
    """Fire one watcher tick now, in a background thread.

    Called on Spotify OAuth success and on app startup (if Spotify already authed)
    so the user doesn't wait up to 5 minutes for the first auto-discover.
    """
    def _runner():
        try:
            log.info("trigger_immediate_watcher: running")
            watch_for_changes(include_deletions=False)
        except Exception:
            log.exception("trigger_immediate_watcher failed")
    threading.Thread(target=_runner, daemon=True).start()


# ====================================================================
# Recompiler: reorder EVERY Plex playlist to match its Spotify source order.
# Manual button + nightly job. Decoupled — reads the local cache, not live Spotify.
# ====================================================================
def recompile_all_plex_playlists(job_id: Optional[int] = None, wait: bool = False) -> dict:
    """Rebuild EVERY Plex-mirrored playlist into Spotify source order (decoupled — works
    from the local cache, no live Spotify). Intensive: meant for the nightly run / manual
    'Recompile' button. Each playlist is cleared and re-added in the cached Spotify order."""
    out = {"playlists": 0, "ok": 0, "errors": 0, "plex_tracks": 0}
    with session_scope() as s:
        pairs = [(p.id, p.name) for p in s.scalars(select(PlaylistPair)).all()
                 if p.plex_enabled and p.plex_playlist_key]
    out["playlists"] = len(pairs)
    for pid, name in pairs:
        try:
            r = rebuild_destinations(pid, do_plex=True, use_cache=True, job_id=job_id, wait=wait)
            if (r.get("status") or "ok") == "error":
                out["errors"] += 1
            else:
                out["ok"] += 1
                out["plex_tracks"] += r.get("rebuilt_plex", 0)
        except Exception:
            out["errors"] += 1
            log.exception("recompile_all: pair %d (%s) failed", pid, name)
    log.info("recompile_all_plex_playlists: %d playlists, %d ok, %d errors, %d tracks reordered",
             out["playlists"], out["ok"], out["errors"], out["plex_tracks"])
    return out


_SMART_RECOMPILE_LOCK = threading.Lock()
_PLAYLIST_SIG_SEEN: dict = {}


def _plex_playlist_sig(plex_key):
    """A CHEAP signature of a Plex playlist — its track count via one light metadata read
    (leafCount), NOT a full fetch of every track (that times out on big playlists). A song
    added/removed changes the count → we re-sort; our own recompile keeps the count the
    same → no thrash. (The nightly full recompile covers the rare add+remove-same-count.)"""
    try:
        srv = plex_client._connect()
        if not srv:
            return None
        pl = srv.fetchItem(int(plex_key))
        n = getattr(pl, "leafCount", None)
        if n is None:
            n = len(pl.items())
        return f"n{int(n)}"
    except Exception:
        return None


def smart_playlist_recompile_tick(job_id: Optional[int] = None) -> dict:
    """SMART auto-recompile: re-applies a playlist's sort order ONLY when its Plex content
    actually changed (a song was added/removed) AND has settled for a tick (debounce), and
    ONLY for non-'source' sort modes ('source' needs no re-sort — new songs just append in
    Spotify order). Per-playlist, decoupled (reads the on-prem Plex set), single-flight —
    so it never thrashes or touches unchanged playlists."""
    out = {"checked": 0, "recompiled": 0, "unchanged": 0, "settling": 0}
    if not _SMART_RECOMPILE_LOCK.acquire(blocking=False):
        out["busy"] = True
        return out
    try:
        with session_scope() as s:
            meta = [(p.id, p.plex_playlist_key, (p.sort_mode or "source"), p.last_sorted_sig)
                    for p in s.scalars(select(PlaylistPair)).all()
                    if p.plex_enabled and p.plex_playlist_key]
        for pid, pkey, mode, last_sig in meta:
            if mode == "source":
                continue  # source order needs no re-sort when a song is appended
            out["checked"] += 1
            cur = _plex_playlist_sig(pkey)
            if cur is None:
                continue
            prev_seen = _PLAYLIST_SIG_SEEN.get(pid)
            _PLAYLIST_SIG_SEEN[pid] = cur
            if cur == last_sig:
                out["unchanged"] += 1
                continue
            if cur != prev_seen:
                # changed since last tick → let it settle one more tick (debounce active syncs)
                out["settling"] += 1
                continue
            # content stable AND different from what we last sorted → re-sort this one
            try:
                rebuild_destinations(pid, do_plex=True, use_cache=True, job_id=job_id)
                new_sig = _plex_playlist_sig(pkey) or cur
                _PLAYLIST_SIG_SEEN[pid] = new_sig
                with session_scope() as s:
                    p = s.get(PlaylistPair, pid)
                    if p:
                        p.last_sorted_sig = new_sig
                out["recompiled"] += 1
            except Exception:
                log.exception("smart_playlist_recompile: pair %d failed", pid)
    finally:
        try:
            _SMART_RECOMPILE_LOCK.release()
        except Exception:
            pass
    if out["recompiled"]:
        log.info("smart_playlist_recompile: %s", out)
    return out


# ====================================================================
# Destructive rebuild: re-order destination playlist to match Spotify source.
# Used when historic adds happened in non-source order.
# ====================================================================
def rebuild_destinations(pair_id: int, *, do_plex: bool = True,
                         use_cache: bool = True,
                         job_id: Optional[int] = None,
                         wait: bool = False, lock_timeout: float = 300.0) -> dict:
    """Destructively rebuild destination playlists to match Spotify's source order.

    By default (use_cache=True), reads the locally-stored snapshot of the
    Spotify playlist — NO Spotify API calls. This is the right choice when
    you just want to fix destination order and don't need to pick up new tracks.

    With use_cache=False, fetches the live Spotify playlist (used when Spotify
    has new tracks since the last snapshot).
    """
    summary = {"rebuilt_plex": 0, "status": "ok", "error": None}
    lock = _pair_lock(pair_id)
    # USER-INITIATED recompiles pass wait=True so a "recompile order" click works even
    # while the pipeline is busy adding songs (a background sync holds this pair lock).
    # Block up to lock_timeout, grabbing the lock the instant the running op frees it,
    # instead of skipping. Background callers keep wait=False (skip + retry next tick).
    if wait:
        if job_id:
            _emit(job_id, f"pair {pair_id}: waiting for in-progress sync to finish\u2026")
        acquired = lock.acquire(blocking=True, timeout=lock_timeout)
    else:
        acquired = lock.acquire(blocking=False)
    if not acquired:
        summary["status"] = "skipped"
        summary["error"] = ("timed out waiting for in-progress sync" if wait
                            else "another sync is in progress for this pair")
        _emit(job_id, f"pair {pair_id}: skipped ({'timed out' if wait else 'already running'})", level="warning")
        return summary
    try:
        with session_scope() as s:
            pair = s.get(PlaylistPair, pair_id)
            if not pair:
                summary["status"] = "error"; summary["error"] = "pair not found"
                return summary
            sp_id = pair.spotify_playlist_id
            plex_key = pair.plex_playlist_key
            pair_name = pair.name
            sort_mode = (getattr(pair, "sort_mode", None) or "").strip().lower()
            if not sort_mode:
                sort_mode = "recent" if bool(getattr(pair, "reverse_order", False)) else "source"
        if not sp_id:
            summary["status"] = "error"; summary["error"] = "no Spotify source on this pair"
            return summary

        # Load track order from local snapshot if available + caller wants cache.
        sp_order: list[str] = []
        sp_by_id: dict = {}
        if use_cache:
            cached_ids = _load_snapshot(pair_id, "spotify")
            if cached_ids:
                _emit(job_id, f"[{pair_name}] rebuild: using local snapshot ({len(cached_ids)} tracks, no Spotify API call)")
                sp_order = list(cached_ids)
                # Fetch track details from TrackMapping cache (we have title/artist there)
                with session_scope() as s:
                    rows = list(s.scalars(
                        select(TrackMapping).where(TrackMapping.spotify_track_id.in_(sp_order))
                    ).all())
                    # Build a minimal "Spotify Track" dict-like for each ID we have details for.
                    # Tracks without TrackMapping entries get a stub (title/artist unknown).
                    class _CachedSpTrack:
                        __slots__ = ("id", "title", "artist", "album", "isrc", "duration_ms")
                        def __init__(self, id, title="", artist="", isrc=None):
                            self.id = id; self.title = title; self.artist = artist
                            self.album = None; self.isrc = isrc; self.duration_ms = 0
                    by_sp_id = {r.spotify_track_id: r for r in rows if r.spotify_track_id}
                    for tid in sp_order:
                        r = by_sp_id.get(tid)
                        sp_by_id[tid] = _CachedSpTrack(tid,
                            title=(r.title if r else ""),
                            artist=(r.artist if r else ""),
                            isrc=(r.isrc if r else None))
        if not sp_order:
            # Cache miss → live fetch (requires Spotify auth)
            if not auth_spotify.is_authed():
                summary["status"] = "error"
                summary["error"] = "no cached snapshot AND Spotify not authed; can't rebuild"
                return summary
            _emit(job_id, f"[{pair_name}] rebuild: no cache; fetching from Spotify…")
            try:
                sp_tracks = spotify_client.get_playlist_tracks(sp_id)
            except spotify_client.PlaylistForbiddenError as e:
                summary["status"] = "error"; summary["error"] = str(e)[:300]
                return summary
            sp_order = [t.id for t in sp_tracks]
            sp_by_id = {t.id: t for t in sp_tracks}

        total = len(sp_order)
        # Two phases × total tracks each = 2*total units of work to surface in the bar.
        if job_id:
            jobs.progress(job_id, current=0,
                          total=total or 1,
                          step=f"Rebuild starting — {total} tracks")
        progress_offset = 0


        if do_plex and plex_key and plex_client.is_authed():
            _emit(job_id, f"[{pair_name}] rebuild Plex: resolving {total} tracks (FLAC only)…")
            plex_keys_in_order: list[str] = []
            plex_misses = 0
            # Per-playlist sort (Spotify-style dropdown): order the PLEX build only — the
            # drift-detection snapshot below stays in true Spotify source order.
            if sort_mode == "recent":
                _plex_source_order = list(reversed(sp_order))
            elif sort_mode in ("title", "artist", "album"):
                def _sk(tid):
                    t = sp_by_id.get(tid)
                    return (str(getattr(t, sort_mode, "") or "").lower(), )
                _plex_source_order = sorted(sp_order, key=_sk)
            else:
                _plex_source_order = sp_order
            if sort_mode and sort_mode != "source":
                _emit(job_id, f"[{pair_name}] sort = {sort_mode} (Plex order)")
            for i, sp_track_id in enumerate(_plex_source_order, 1):
                sp_t = sp_by_id.get(sp_track_id)
                if not sp_t:
                    continue
                if job_id and (i % 10 == 0 or i == total):
                    jobs.progress(
                        job_id, current=progress_offset + i,
                        step=f"Plex {i}/{total}: {sp_t.title[:50]} — {sp_t.artist[:40]} "
                             f"({len(plex_keys_in_order)} matched, {plex_misses} miss)",
                    )
                pkey = _resolve_to_plex(sp_t)
                if pkey:
                    plex_keys_in_order.append(pkey)
                else:
                    plex_misses += 1
            current = plex_client.get_playlist_tracks(plex_key)
            if current:
                _emit(job_id, f"[{pair_name}] clearing {len(current)} from Plex…")
                plex_client.remove_tracks(plex_key, [t.key for t in current])
            if plex_keys_in_order:
                _emit(job_id, f"[{pair_name}] adding {len(plex_keys_in_order)} to Plex in source order…")
                added = plex_client.add_tracks(plex_key, plex_keys_in_order)
                summary["rebuilt_plex"] = len(added)
            _save_snapshot(pair_id, "plex", plex_keys_in_order)

        _save_snapshot(pair_id, "spotify", sp_order)
        _emit(job_id, f"[{pair_name}] rebuild done: +{summary['rebuilt_plex']} Plex")
    except Exception as e:
        log.exception("[%s] rebuild failed: %s", pair_name, e)
        summary["status"] = "error"; summary["error"] = str(e)[:500]
        _emit(job_id, f"[{pair_name}] rebuild failed: {e}", level="error")
    finally:
        lock.release()
    return summary
