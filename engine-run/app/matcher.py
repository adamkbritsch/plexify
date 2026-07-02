"""Track matching across services.

Strategy:
  1. ISRC exact (when both sides expose it).
  2. Fuzzy title + artist with duration proximity boost.
Match results are cached in TrackMapping.
"""
import logging
import re
from typing import Optional

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from rapidfuzz import fuzz
from sqlalchemy import desc, select

from .config import FUZZY_ARTIST_THRESHOLD, FUZZY_DURATION_TOLERANCE_MS, FUZZY_TITLE_THRESHOLD
from .db import TrackMapping, session_scope

log = logging.getLogger(__name__)

_PAREN = re.compile(r"\s*[\(\[].*?[\)\]]")
_FEAT = re.compile(r"\s*\b(feat\.?|featuring|ft\.?)\b.*", re.IGNORECASE)  # Q18: dropped "with" — breaks legit titles like "With or Without You"
_NONALNUM = re.compile(r"[^\w\s]")


def _norm(s: str) -> str:
    if not s:
        return ""
    s = s.lower()
    s = _PAREN.sub("", s)
    s = _FEAT.sub("", s)
    s = _NONALNUM.sub(" ", s)
    return " ".join(s.split())


def score(query_title: str, query_artist: str, query_dur_ms: Optional[int], cand) -> int:
    """Return a confidence value. Higher is better; values >100 indicate a duration tie-break boost."""
    t = fuzz.token_set_ratio(_norm(query_title), _norm(cand.title))
    a = fuzz.token_set_ratio(_norm(query_artist), _norm(cand.artist))
    if t < FUZZY_TITLE_THRESHOLD or a < FUZZY_ARTIST_THRESHOLD:
        return 0
    base = int(0.6 * t + 0.4 * a)
    if query_dur_ms and cand.duration_ms:
        diff = abs(query_dur_ms - cand.duration_ms)
        if diff <= FUZZY_DURATION_TOLERANCE_MS:
            base += 5
        elif diff > 10000:
            base = max(0, base - 15)
    return base


def pick_best(title: str, artist: str, duration_ms: Optional[int], candidates: list) -> Optional[object]:
    """Pick the highest-scoring candidate that clears the bar, else None."""
    best = None
    best_score = 0
    for c in candidates:
        s = score(title, artist, duration_ms, c)
        if s > best_score:
            best = c
            best_score = s
    return best if best_score >= FUZZY_TITLE_THRESHOLD else None


@dataclass
class CachedMapping:
    spotify_track_id: Optional[str]
    plex_track_key: Optional[str]
    isrc: Optional[str]
    method: str
    confidence: int
    plex_searched_at: Optional["datetime"] = None  # for negative cache


# Re-search Plex for a track if our last attempt was longer ago than this and
# we still didn't find a match. User might have added FLACs via Lidarr since.
PLEX_NEGATIVE_CACHE_TTL_HOURS = 24


def lookup_cached(spotify_id: Optional[str] = None,
                  plex_key: Optional[str] = None) -> Optional[CachedMapping]:
    if not (spotify_id or plex_key):
        return None
    with session_scope() as s:
        q = select(TrackMapping)
        if spotify_id:
            q = q.where(TrackMapping.spotify_track_id == spotify_id)
        if plex_key:
            q = q.where(TrackMapping.plex_track_key == plex_key)
        q = q.order_by(desc(TrackMapping.confidence), desc(TrackMapping.created_at)).limit(1)
        row = s.scalar(q)
        if not row:
            return None
        return CachedMapping(
            spotify_track_id=row.spotify_track_id,
            plex_track_key=row.plex_track_key,
            isrc=row.isrc,
            method=row.method,
            confidence=row.confidence,
            plex_searched_at=row.plex_searched_at,
        )


def plex_search_was_recent(cached: Optional[CachedMapping]) -> bool:
    """Negative cache check: did we already search Plex for this track recently?

    If True, the caller should skip re-searching — the previous search returned
    no FLAC match. After PLEX_NEGATIVE_CACHE_TTL_HOURS, search again (user may
    have downloaded a FLAC since).
    """
    if not cached or not cached.plex_searched_at:
        return False
    age = datetime.now(timezone.utc).replace(tzinfo=None) - cached.plex_searched_at
    return age < timedelta(hours=PLEX_NEGATIVE_CACHE_TTL_HOURS)


def mark_plex_search_attempted(spotify_id: str) -> None:
    """Record a Plex search attempt (even on miss) for the negative-cache."""
    if not spotify_id:
        return
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as s:
        row = s.scalar(
            select(TrackMapping)
            .where(TrackMapping.spotify_track_id == spotify_id)
            .order_by(desc(TrackMapping.confidence), desc(TrackMapping.created_at))
            .limit(1)
        )
        if row:
            row.plex_searched_at = now
        else:
            # No existing row — create a stub so we remember we tried.
            s.add(TrackMapping(
                spotify_track_id=spotify_id,
                title="", artist="", method="plex_miss",
                confidence=0, plex_searched_at=now,
            ))


def save_mapping(spotify_id: Optional[str] = None,
                 plex_key: Optional[str] = None,
                 isrc: Optional[str] = None,
                 title: str = "",
                 artist: str = "",
                 method: str = "fuzzy",
                 confidence: int = 90) -> None:
    """Upsert a track mapping. Any of the two service IDs may be set.

    If a row already exists matching the spotify_id, missing fields
    on that row are filled in. Otherwise a new row is created.
    """
    if not (spotify_id or plex_key):
        return
    with session_scope() as s:
        existing = None
        if spotify_id:
            existing = s.scalar(
                select(TrackMapping)
                .where(TrackMapping.spotify_track_id == spotify_id)
                .order_by(desc(TrackMapping.confidence), desc(TrackMapping.created_at))
                .limit(1)
            )
        if not existing and plex_key:
            existing = s.scalar(
                select(TrackMapping)
                .where(TrackMapping.plex_track_key == plex_key)
                .order_by(desc(TrackMapping.confidence), desc(TrackMapping.created_at))
                .limit(1)
            )
        if existing:
            if spotify_id and not existing.spotify_track_id:
                existing.spotify_track_id = spotify_id
            if plex_key and not existing.plex_track_key:
                existing.plex_track_key = plex_key
            if isrc and not existing.isrc:
                existing.isrc = isrc
            # Q26: keep (method, confidence) consistent. Two upgrade paths:
            #   1. higher confidence wins both
            #   2. switching TO 'isrc' wins (it's a hard match)
            if confidence > existing.confidence:
                existing.method = method
                existing.confidence = confidence
            elif method == "isrc" and existing.method != "isrc":
                existing.method = "isrc"
                existing.confidence = max(existing.confidence, confidence)
            return
        s.add(TrackMapping(
            spotify_track_id=spotify_id,
            plex_track_key=plex_key,
            isrc=isrc,
            title=(title or "")[:500],
            artist=(artist or "")[:500],
            method=method,
            confidence=confidence,
        ))
