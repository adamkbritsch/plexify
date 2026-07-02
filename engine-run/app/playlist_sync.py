"""Bidirectional Spotify <-> Plex playlist sync (3-way merge with baseline).

For every enabled playlist pair this reconciles song *membership* in both directions:

  * a song added on EITHER platform appears on both, and
  * a song removed on EITHER platform is removed from both (mirror removals).

It uses a per-pair baseline (the set of Spotify track ids last known to be in sync)
so it can tell an add from a remove on each side — a proper 3-way merge rather than
"make B look like A". The Spotify track id is the canonical identity; Plex tracks map
to Spotify ids via TrackMapping with a search fallback.

Spotify-playlist songs that aren't in the Plex library yet are queued for download
(an AutofillAction) so they land on the Plex side once acquired.

Removals are logged to a rollback manifest under /data so any mirror-removal can be
undone.

Order is left to the nightly recompile (Spotify order -> Plex); this module owns
membership.
"""
from __future__ import annotations

import json
import logging
import threading
import time

from sqlalchemy import select

from . import plex_client, spotify_client
from .autofill_engine import LIKED_SONGS_SENTINEL, _record_action_in_session
from .db import (PlaylistPair, SessionLocal, TrackMapping, get_config,
                 session_scope)

log = logging.getLogger(__name__)

_LOCK = threading.Lock()


def _sig(keys) -> str:
    import hashlib
    h = hashlib.sha1("\n".join(sorted(str(k) for k in keys)).encode("utf-8")).hexdigest()
    return h[:32]


def _spotify_can_write_playlists() -> bool:
    """True only if the connected Spotify token was granted playlist-modify. Until the
    user reconnects with the updated scopes, writing to Spotify 403s, so we keep the
    whole bidirectional sync a clean no-op rather than half-applying / queueing."""
    try:
        from .db import AuthToken
        with SessionLocal() as s:
            row = s.get(AuthToken, "spotify")
            if not row:
                return False
            scope = (json.loads(row.payload or "{}") or {}).get("scope", "") or ""
        return "playlist-modify" in scope
    except Exception:
        return False


def _reverse_map_plex_to_spotify(plex_tracks):
    """plex_tracks -> (sid_set, sid->plex_key, unresolved_plex_keys).

    Identity by TrackMapping first (exact), Spotify search fallback for the rest."""
    keys = [t.key for t in plex_tracks]
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
    sid_set = set()
    sid_to_key = {}
    unresolved = []
    for t in plex_tracks:
        sid = rev.get(str(t.key))
        if not sid:
            try:
                hit = spotify_client.search_track(t.title or "", t.artist or "")
                sid = getattr(hit, "id", None) if hit else None
            except Exception:
                sid = None
        if sid:
            sid_set.add(sid)
            sid_to_key.setdefault(sid, str(t.key))
        else:
            unresolved.append(str(t.key))
    return sid_set, sid_to_key, unresolved


def _resolve_spotify_to_plex_key(sid: str) -> str | None:
    with SessionLocal() as s:
        m = s.scalar(select(TrackMapping)
                     .where(TrackMapping.spotify_track_id == sid)
                     .where(TrackMapping.plex_track_key.isnot(None)))
        if m:
            return str(m.plex_track_key)
    return None


def sync_playlist_pair(pair_id: int, *, force: bool = False) -> dict:
    out = {"pair_id": pair_id, "name": None, "added_spotify": 0, "removed_spotify": 0,
           "added_plex": 0, "removed_plex": 0, "queued_download": 0,
           "unresolved_plex": 0, "needs_reconnect": False, "skipped": None,
           "removals": []}
    with SessionLocal() as s:
        pair = s.get(PlaylistPair, pair_id)
        if not pair:
            out["skipped"] = "no pair"
            return out
        out["name"] = pair.name
        sp_id = pair.spotify_playlist_id
        plex_key = pair.plex_playlist_key
        baseline = set(json.loads(pair.sync_baseline_json or "[]"))
        last_plex_sig = pair.last_plex_sig
        last_snap = pair.last_spotify_snapshot_id
    if not sp_id or sp_id == LIKED_SONGS_SENTINEL or not plex_key or not pair.plex_enabled:
        out["skipped"] = "not a bidirectional pair"
        return out

    # --- current states ---
    try:
        snap = spotify_client.get_playlist_snapshot_id(sp_id)
    except Exception:
        snap = None
    try:
        plex_tracks = plex_client.get_playlist_tracks(plex_key)
    except Exception:
        log.exception("playlist_sync: cannot read Plex playlist %s", plex_key)
        out["skipped"] = "plex read failed"
        return out
    plex_sig = _sig(t.key for t in plex_tracks)

    # Cheap skip when nothing changed on either side since last sync.
    if (not force and snap and last_snap == snap and last_plex_sig == plex_sig
            and pair.sync_baseline_json is not None):
        out["skipped"] = "unchanged"
        return out

    try:
        sp_tracks = spotify_client.get_playlist_tracks(sp_id)
    except Exception:
        log.exception("playlist_sync: cannot read Spotify playlist %s", sp_id)
        out["skipped"] = "spotify read failed"
        return out
    S = {t.id for t in sp_tracks if t.id}
    sp_meta = {t.id: t for t in sp_tracks if t.id}

    P, p_sid_to_key, unresolved = _reverse_map_plex_to_spotify(plex_tracks)
    out["unresolved_plex"] = len(unresolved)
    B = baseline

    # --- 3-way merge (membership) ---
    # First sync (no baseline yet) is a pure UNION so we never delete on a cold start.
    cold_start = pair.sync_baseline_json is None
    desired = set()
    for x in (S | P):
        if cold_start:
            desired.add(x)
        elif x not in B:
            desired.add(x)            # added on some side since last sync
        elif x in S and x in P:
            desired.add(x)            # still present on both -> keep
        # else: in B but missing on one side -> removed -> drop

    # --- apply: Spotify side FIRST — it gates everything else. If we can't write to
    # Spotify (e.g. the token still lacks playlist-modify until a reconnect), abort
    # WITHOUT touching Plex or the baseline, so we never mirror a removal we couldn't
    # actually propagate (which would otherwise delete the song from Plex next run). ---
    to_add_sp = [x for x in desired if x not in S]
    to_rem_sp = [x for x in S if x not in desired]
    if to_add_sp:
        added = spotify_client.add_tracks(sp_id, to_add_sp)
        out["added_spotify"] = len(added)
        if not added:
            out["needs_reconnect"] = True
            out["skipped"] = "spotify write blocked — reconnect Spotify"
            return out
    if to_rem_sp:
        removed = spotify_client.remove_tracks(sp_id, to_rem_sp)
        if not removed:
            out["needs_reconnect"] = True
            out["skipped"] = "spotify write blocked — reconnect Spotify"
            return out
        out["removed_spotify"] = len(removed)
        for sid in removed:
            out["removals"].append({"side": "spotify", "playlist": pair.name, "sid": sid})

    # --- apply: Plex side ---
    to_add_plex = [x for x in desired if x not in P]
    to_rem_plex = [x for x in P if x not in desired]
    plex_keys_to_add = []
    queue_download = []
    # On small delta syncs we can afford a live Plex search to catch songs that are in
    # the library but unmapped. On a big cold-start backfill (hundreds of songs) that
    # would be far too slow, so trust the exact download mapping and queue the rest.
    do_search = len(to_add_plex) <= 50
    for sid in to_add_plex:
        k = _resolve_spotify_to_plex_key(sid)
        if not k and do_search:
            meta = sp_meta.get(sid)
            if meta:
                try:
                    hit = plex_client.search_track(meta.title or "", meta.artist or "")
                    k = getattr(hit, "key", None) if hit else None
                except Exception:
                    k = None
        if k:
            plex_keys_to_add.append(str(k))
        else:
            queue_download.append(sid)
    if plex_keys_to_add:
        added = plex_client.add_tracks(plex_key, plex_keys_to_add)
        out["added_plex"] = len(added)
    if to_rem_plex:
        keys = [p_sid_to_key[sid] for sid in to_rem_plex if sid in p_sid_to_key]
        if keys:
            removed = plex_client.remove_tracks(plex_key, keys)
            out["removed_plex"] = len(removed)
            for sid in to_rem_plex:
                out["removals"].append({"side": "plex", "playlist": pair.name,
                                        "sid": sid, "plex_key": p_sid_to_key.get(sid)})

    # --- download songs that should be on Plex but aren't in the library ---
    # The autofill table is ALBUM-keyed (one row per album), so group the missing
    # tracks by (artist, album) and enqueue one action per album with the combined
    # track ids. Each group commits in its own session so one collision can't abort
    # the batch.
    if queue_download and (get_config("playlist_sync_download_missing", "1") or "1") == "1":
        groups: dict = {}
        for sid in queue_download:
            meta = sp_meta.get(sid)
            if not meta:
                continue
            k = (meta.artist or "Unknown", meta.album or (meta.title or "Unknown"))
            groups.setdefault(k, set()).add(sid)
        queued = 0
        for (art, alb), sids in groups.items():
            try:
                with session_scope() as s:
                    _record_action_in_session(s, artist=art, album=alb, status="queued",
                                              track_ids=sids, note=f"playlist sync: {pair.name}")
                queued += len(sids)
            except Exception:
                log.exception("playlist_sync: enqueue failed for %s / %s", art, alb)
        out["queued_download"] = queued

    # --- persist new baseline + signatures ---
    with SessionLocal() as s:
        pair = s.get(PlaylistPair, pair_id)
        if pair:
            pair.sync_baseline_json = json.dumps(sorted(desired))
            # Re-read post-sync signatures so the next cycle sees "unchanged".
            try:
                pair.last_spotify_snapshot_id = spotify_client.get_playlist_snapshot_id(sp_id) or snap
            except Exception:
                pair.last_spotify_snapshot_id = snap
            try:
                pair.last_plex_sig = _sig(t.key for t in plex_client.get_playlist_tracks(plex_key))
            except Exception:
                pair.last_plex_sig = plex_sig
            s.commit()
    return out


def sync_all_playlists_bidirectional(job_id: int | None = None, *, force: bool = False) -> dict:
    """Run the bidirectional merge across every enabled pair. Single-flight."""
    summary = {"pairs": 0, "synced": 0, "added_spotify": 0, "removed_spotify": 0,
               "added_plex": 0, "removed_plex": 0, "queued_download": 0, "skipped": 0}
    if (get_config("playlist_bidirectional_enabled", "1") or "1") != "1":
        summary["disabled"] = True
        return summary
    if not _spotify_can_write_playlists():
        # Spotify token can't write playlists yet — reconnect needed. No-op until then
        # so we never half-sync or queue downloads we can't complete bidirectionally.
        summary["needs_reconnect"] = True
        log.info("playlist_sync: skipped — Spotify reconnect needed for playlist-modify")
        return summary
    if not _LOCK.acquire(blocking=False):
        summary["skipped_busy"] = True
        return summary
    all_removals = []
    try:
        with SessionLocal() as s:
            pair_ids = [p.id for p in s.scalars(
                select(PlaylistPair)
                .where(PlaylistPair.plex_enabled == True)  # noqa: E712
                .where(PlaylistPair.plex_playlist_key.isnot(None))
                .where(PlaylistPair.spotify_playlist_id.isnot(None))
                .where(PlaylistPair.spotify_playlist_id != LIKED_SONGS_SENTINEL)
            ).all()]
        for pid in pair_ids:
            summary["pairs"] += 1
            try:
                r = sync_playlist_pair(pid, force=force)
            except Exception:
                log.exception("playlist_sync: pair %s failed", pid)
                continue
            if r.get("skipped"):
                summary["skipped"] += 1
                continue
            summary["synced"] += 1
            for k in ("added_spotify", "removed_spotify", "added_plex", "removed_plex", "queued_download"):
                summary[k] += r.get(k, 0)
            all_removals.extend(r.get("removals", []))
        # Rollback manifest for any mirror removals this run.
        if all_removals:
            ts = time.strftime("%Y%m%d_%H%M%S")
            path = f"/data/playlist_sync_rollback_{ts}.json"
            try:
                with open(path, "w") as fh:
                    json.dump({"ts": ts, "removals": all_removals}, fh, indent=2)
                summary["rollback_manifest"] = path
            except Exception:
                log.exception("playlist_sync: could not write rollback manifest")
    finally:
        try:
            _LOCK.release()
        except Exception:
            pass
    log.info("playlist_sync (bidirectional): %s", summary)
    return summary
