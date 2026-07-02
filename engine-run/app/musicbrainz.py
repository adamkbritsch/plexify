"""MusicBrainz lookup — the authoritative 'mdb' for canonical album metadata.

This module is the ONLY live MusicBrainz caller and is invoked exclusively from the
gentle background ``mb_enrich_tick`` (never the acquisition hot path). It respects
MusicBrainz etiquette: a descriptive User-Agent (required, else 403) and ≤1 request
per second. Results are cached in the ``mb_album_meta`` table so the picker reads a
local answer instead of hitting the network.

MusicBrainz models exactly what we need: compilations/soundtracks are credited to a
dedicated 'Various Artists' entity, release-groups carry primary type (Album/Single/
EP…) and secondary types (Soundtrack/Compilation/Live…), and every release-group has
an authoritative artist credit. So we read the canonical album artist instead of
guessing it from album-name keywords.
"""
from __future__ import annotations

import logging
import re
import threading
import time

import requests

log = logging.getLogger(__name__)

MB_BASE = "https://musicbrainz.org/ws/2"
# MusicBrainz' special-purpose artist for compilations with no single album artist.
VA_MBID = "89ad4ac3-39f7-470e-963a-56509c546377"

_RATE_LOCK = threading.Lock()
_last_call = [0.0]
_MIN_INTERVAL = 1.1  # seconds between live MB requests (their guideline is ~1/sec)


def _norm(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[\s\-_]+", " ", s)
    s = re.sub(r"[^\w\s]", "", s)
    return s.strip()


_TRAIL_PAREN = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*$")


def _core_title(s: str) -> str:
    """Strip trailing parenthetical/bracket suffixes that streaming services append
    but MusicBrainz's canonical title usually omits — '(Motion Picture Soundtrack)',
    '(Deluxe Edition)', '(Original Broadway Cast Recording)', etc."""
    prev = None
    s = (s or "").strip()
    while prev != s:
        prev = s
        s = _TRAIL_PAREN.sub("", s).strip()
    return s or (prev or "")


def _user_agent() -> str:
    try:
        from .db import get_config
        ua = get_config("musicbrainz_user_agent", None)
        if ua:
            return ua
    except Exception:
        pass
    return "Plexify/1.0 (Spotify->Plex FLAC backup; https://github.com/plexify)"


def _lucene_escape(s: str) -> str:
    """Strip Lucene operators/quotes so a free-text album/artist can't break the query."""
    s = (s or "").replace("\\", " ").replace('"', " ")
    s = re.sub(r"[+\-!(){}\[\]^~*?:/]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _rate_wait() -> None:
    with _RATE_LOCK:
        dt = time.monotonic() - _last_call[0]
        if dt < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - dt)
        _last_call[0] = time.monotonic()


def _credit_phrase(artist_credit):
    """Flatten a MusicBrainz artist-credit into ('Primary feat. Guest', [mbids])."""
    parts, mbids = [], []
    for c in artist_credit or []:
        nm = c.get("name") or (c.get("artist") or {}).get("name") or ""
        parts.append(nm + (c.get("joinphrase") or ""))
        aid = (c.get("artist") or {}).get("id")
        if aid:
            mbids.append(aid)
    return "".join(parts).strip(), mbids


def _query_release_groups(lucene: str, limit: int = 8):
    # MB's search server can be sluggish on a cold hit; one retry handles the
    # occasional slow response without aborting the background enrichment pass.
    last_err = None
    for attempt in range(2):
        _rate_wait()
        try:
            r = requests.get(
                f"{MB_BASE}/release-group/",
                params={"query": lucene, "fmt": "json", "limit": limit},
                headers={"User-Agent": _user_agent(), "Accept": "application/json"},
                timeout=25,
            )
        except Exception as e:
            last_err = e
            time.sleep(1.5)
            continue
        if r.status_code == 503:
            log.warning("musicbrainz: 503 (rate limited) for query=%r", lucene)
            time.sleep(1.5)
            continue
        if r.status_code != 200:
            log.warning("musicbrainz: HTTP %s for query=%r", r.status_code, lucene)
            return None
        try:
            return r.json().get("release-groups", []) or []
        except Exception:
            return None
    if last_err is not None:
        log.warning("musicbrainz: request failed for query=%r (%s)", lucene, last_err)
    return None


def mb_lookup_album(artist: str, album: str) -> dict | None:
    """Look up the canonical album metadata for (artist, album) in MusicBrainz.

    Returns a dict (album_artist, album_artist_mbid, is_various, release_group_mbid,
    primary_type, secondary_types, score) or None when there's no confident match.
    """
    alb_n = _norm(album)
    alb_core = _norm(_core_title(album))   # without trailing '(... Soundtrack)' etc.
    if not alb_n:
        return None
    # Query with the CORE title — MB's canonical title rarely carries the streaming
    # suffix, so searching the core finds soundtracks like 'Dr. Horrible's Sing-Along
    # Blog' even when Spotify calls it '... (Motion Picture Soundtrack)'.
    qa = _lucene_escape(_core_title(album)) or _lucene_escape(album)
    qr = _lucene_escape(artist)

    def title_ok(rg):
        t = _norm(rg.get("title"))
        if not t:
            return False
        # exact (full or core), or one is a strong substring of the other
        return (t == alb_n or t == alb_core
                or (alb_core and (alb_core in t or t in alb_core)))

    candidates = []
    # 1) Constrained by artist (best for normal albums credited to that artist).
    if qr:
        got = _query_release_groups(f'releasegroup:"{qa}" AND artist:"{qr}"')
        if got:
            candidates.extend(got)
    # 2) If no good title hit yet, broaden to title-only — this is what catches
    #    soundtracks/compilations credited to 'Various Artists' (not the performer).
    if not any(title_ok(rg) for rg in candidates):
        got = _query_release_groups(f'releasegroup:"{qa}"')
        if got:
            candidates.extend(got)
    if not candidates:
        return None

    _NONMUSIC = ("audiobook", "audio book", "spokenword", "spoken word",
                 "interview", "audio drama")

    def _is_nonmusic(rg):
        sec = [str(x).lower() for x in (rg.get("secondary-types") or [])]
        return any(any(nm in x for nm in _NONMUSIC) for x in sec)

    def rank(rg):
        t = _norm(rg.get("title"))
        sec = [str(x).lower() for x in (rg.get("secondary-types") or [])]
        phrase, _m = _credit_phrase(rg.get("artist-credit"))
        pn = _norm(phrase)
        # Push down non-music release-groups (the 'Book of Mormon' audiobook trap)
        # and '[unknown]' credits; lift exact-title + soundtrack/compilation matches.
        music = 0 if _is_nonmusic(rg) else 1
        good_artist = 0 if pn in ("", "unknown") else 1
        exact = 2 if (t == alb_n or t == alb_core) else (1 if title_ok(rg) else 0)
        soundtracky = 1 if any(x in ("soundtrack", "compilation") for x in sec) else 0
        score = int(rg.get("score") or 0)
        is_album = 1 if (rg.get("primary-type") == "Album") else 0
        return (music, good_artist, exact, soundtracky, score, is_album)

    candidates.sort(key=rank, reverse=True)
    best = candidates[0]

    # Never return a non-music match (audiobook/spoken word) — better to say 'unknown'.
    if _is_nonmusic(best):
        return None
    # Guard against garbage: require a title match (exact/core/substring), or a very
    # high MB score, before we trust the credit.
    if not title_ok(best) and int(best.get("score") or 0) < 90:
        return None

    phrase, mbids = _credit_phrase(best.get("artist-credit"))
    is_va = (VA_MBID in mbids) or (_norm(phrase) == "various artists")
    secondary = best.get("secondary-types") or []
    return {
        "album_artist": "Various Artists" if is_va else (phrase or None),
        "album_artist_mbid": (mbids[0] if mbids else None),
        "is_various": is_va,
        "release_group_mbid": best.get("id"),
        "primary_type": best.get("primary-type"),
        "secondary_types": ",".join(secondary),
        "score": int(best.get("score") or 0),
    }
