"""Coverage-gap suggestor — ranks WHICH artists' missing tracks, if acquired, would raise Plex
match-coverage the most. Powers the Unmatched page's "what to go get" list: the automated sources
can't get everything, so this tells the user which artist to manually source next for the biggest
coverage win, and flags the tracks the sources have already given up on (the prime manual-import
candidates).

All cheap DB reads over the local mirror (liked songs + playlist mirror vs. Plex match cache) —
no live Spotify/Plex calls, safe to hit on demand.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import select

from .db import (session_scope, get_config, set_config,
                 SpotifyLikedTrack, LocalTrack, TrackMapping, UnmatchedTrack, AutofillAction)

log = logging.getLogger(__name__)

_DISMISSED_KEY = "suggestor_dismissed_json"


def _norm(s: str) -> str:
    from .autofill_engine import _normalize_for_key
    return _normalize_for_key(s or "")


def _dismissed() -> set:
    try:
        return set(json.loads(get_config(_DISMISSED_KEY, "[]") or "[]"))
    except Exception:
        return set()


def coverage_gap_suggestions(min_tracks: int = 3, limit: int = 20) -> dict:
    """Rank artists by how many of the user's WANTED (liked ∪ playlist) tracks are missing from
    Plex. Returns {suggestions:[{artist, artist_key, missing_count, needs_manual_count,
    coverage_gain_pct, missing_albums, sample_titles}], current_coverage_pct, wanted_total,
    covered_total}."""
    with session_scope() as s:
        # 1. Wanted universe: liked ∪ playlist, keyed by spotify_track_id.
        wanted: dict[str, tuple] = {}
        for tid, artist, album, title in s.execute(
                select(SpotifyLikedTrack.spotify_track_id, SpotifyLikedTrack.artist,
                       SpotifyLikedTrack.album, SpotifyLikedTrack.title)).all():
            if tid:
                wanted[tid] = (artist or "", album or "", title or "")
        for tid, artist, album, title in s.execute(
                select(LocalTrack.spotify_track_id, LocalTrack.artist,
                       LocalTrack.album, LocalTrack.title)).all():
            if tid and tid not in wanted:
                wanted[tid] = (artist or "", album or "", title or "")

        # 2. Covered = wanted tracks with a real Plex match.
        covered = {tid for (tid,) in s.execute(
            select(TrackMapping.spotify_track_id).where(
                TrackMapping.plex_track_key.isnot(None),
                TrackMapping.spotify_track_id.isnot(None))).all()}

        # 3. "Sources exhausted" = searched-and-missed (negative cache) + explicit unmatched log.
        exhausted = {tid for (tid,) in s.execute(
            select(TrackMapping.spotify_track_id).where(
                TrackMapping.plex_track_key.is_(None),
                TrackMapping.plex_searched_at.isnot(None),
                TrackMapping.spotify_track_id.isnot(None))).all()}
        for (tid,) in s.execute(
                select(UnmatchedTrack.source_track_id).where(
                    UnmatchedTrack.target_service == "plex")).all():
            if tid:
                exhausted.add(tid)

        total_wanted = len(wanted)
        covered_wanted = sum(1 for tid in wanted if tid in covered)

    # 4. Group uncovered wanted tracks by normalized artist.
    by_artist: dict[str, dict] = {}
    for tid, (artist, album, title) in wanted.items():
        if tid in covered:
            continue
        ak = _norm(artist)
        if not ak:
            continue
        g = by_artist.setdefault(ak, {"artist": artist, "tracks": set(),
                                      "albums": {}, "needs_manual": 0, "samples": []})
        g["tracks"].add(tid)
        ex = tid in exhausted
        if album:
            alb = g["albums"].setdefault(_norm(album),
                                         {"album": album, "tids": set(), "needs_manual": 0})
            alb["tids"].add(tid)
            if ex:
                alb["needs_manual"] += 1
        if ex:
            g["needs_manual"] += 1
        if len(g["samples"]) < 5 and title:
            g["samples"].append(title)

    dismissed = _dismissed()
    denom = total_wanted or 1
    out = []
    for ak, g in by_artist.items():
        if ak in dismissed:
            continue
        c = len(g["tracks"])
        if c < min_tracks:
            continue
        # Per-album breakdown, ranked by how much each album would raise coverage (== missing
        # tracks, since the denominator is fixed). Powers the expandable card's album dropdown.
        albums_ranked = sorted(
            [{"album": a["album"],
              "missing_count": len(a["tids"]),
              "needs_manual_count": a["needs_manual"],
              "coverage_gain_pct": round(100 * len(a["tids"]) / denom, 1)}
             for a in g["albums"].values()],
            key=lambda x: (x["missing_count"], x["needs_manual_count"]), reverse=True)
        out.append({
            "artist": g["artist"],
            "artist_key": ak,
            "missing_count": c,
            "needs_manual_count": g["needs_manual"],
            "coverage_gain_pct": round(100 * c / denom, 1),
            "missing_albums": [a["album"] for a in albums_ranked][:12],   # ranked names (compat)
            "albums": albums_ranked[:25],                                 # rich ranked breakdown
            "sample_titles": g["samples"],
        })
    # Rank by raw uncovered count (== coverage-gain order, denominator is fixed);
    # tie-break toward the ones the sources have given up on (more manual-worthy).
    out.sort(key=lambda x: (x["missing_count"], x["needs_manual_count"]), reverse=True)
    return {
        "suggestions": out[:max(1, limit)],
        "current_coverage_pct": round(100 * covered_wanted / denom),
        "wanted_total": total_wanted,
        "covered_total": covered_wanted,
    }


def dismiss_artist(artist_key: str) -> dict:
    """Hide an artist from future suggestions (persisted in config). Accepts the normalized
    artist_key or a display name (normalized here so either works)."""
    ak = _norm(artist_key)
    if not ak:
        return {"ok": False, "error": "no artist"}
    d = _dismissed()
    d.add(ak)
    set_config(_DISMISSED_KEY, json.dumps(sorted(d)))
    return {"ok": True, "dismissed": ak}


def undismiss_artist(artist_key: str) -> dict:
    d = _dismissed()
    d.discard(_norm(artist_key))
    set_config(_DISMISSED_KEY, json.dumps(sorted(d)))
    return {"ok": True}


def requeue_artist(artist_key: str) -> dict:
    """Give the sources another automated shot at an artist's missing albums: insert a queued
    AutofillAction per (artist, album) that isn't already tracked. Reuses the same key scheme as
    the discography queue so it dedupes against existing rows."""
    ak = _norm(artist_key)
    if not ak:
        return {"ok": False, "error": "no artist"}
    # Rebuild this artist's uncovered (album -> track_ids) from the wanted universe.
    with session_scope() as s:
        covered = {tid for (tid,) in s.execute(
            select(TrackMapping.spotify_track_id).where(
                TrackMapping.plex_track_key.isnot(None),
                TrackMapping.spotify_track_id.isnot(None))).all()}
        wanted = []
        for tid, artist, album in s.execute(
                select(SpotifyLikedTrack.spotify_track_id, SpotifyLikedTrack.artist,
                       SpotifyLikedTrack.album)).all():
            wanted.append((tid, artist or "", album or ""))
        for tid, artist, album in s.execute(
                select(LocalTrack.spotify_track_id, LocalTrack.artist, LocalTrack.album)).all():
            wanted.append((tid, artist or "", album or ""))

        albums: dict[str, dict] = {}   # album_key -> {artist, album, tids}
        for tid, artist, album in wanted:
            if not tid or tid in covered or _norm(artist) != ak or not album:
                continue
            bk = _norm(album)
            a = albums.setdefault(bk, {"artist": artist, "album": album, "tids": set()})
            a["tids"].add(tid)

        existing = {(r_ak, r_bk) for (r_ak, r_bk) in s.execute(
            select(AutofillAction.artist_key, AutofillAction.album_key).where(
                AutofillAction.artist_key == ak)).all()}

        queued = 0
        for bk, a in albums.items():
            if (ak, bk) in existing:
                continue
            s.add(AutofillAction(artist=a["artist"], album=a["album"],
                                 artist_key=ak, album_key=bk, status="queued",
                                 track_ids_json=json.dumps(sorted(a["tids"]))))
            queued += 1
        s.commit()
    return {"ok": True, "queued_albums": queued}
