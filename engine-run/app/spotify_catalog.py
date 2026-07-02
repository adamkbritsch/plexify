"""spotify_catalog.py — the ONLY module that calls Spotify during normal operation.

Gently mirrors the Spotify catalog (each liked-songs artist's albums + tracks, with ISRCs)
into local tables so the consolidation planner and the picker never touch Spotify live.
Resumable, rate-limited, backs off on 429 (via spotify_client._retry_patient).

Spotify gotcha (2026-05): /v1/search now 400s on limit>10 ("Invalid limit"). Search calls
here use limit<=10. Non-search endpoints (artist_albums, album_tracks, tracks) still allow 50.
"""
from __future__ import annotations

import logging
import re
import unicodedata

# Generic hits-compilation SERIES that should never be acquired/created as albums, matched by
# title regardless of how the album-artist is credited (the "Now That's What I Call Music!"
# franchise and friends). These scoop unrelated songs from 50 artists — never a real album.
_BLOCKED_COMP_RE = re.compile(
    r"now\s*!?\s*\d*\s*[:,]?\s*that'?’?s?\s*what\s+i\s+call",   # Now That's What I Call Music!
    re.I)


def is_blocked_comp_name(name: str) -> bool:
    return bool(_BLOCKED_COMP_RE.search(name or ""))


# COVER PRODUCTS — never a real album no matter what Spotify's album_type says
# (Rockabye lullabies, "Renditions of X", tribute/karaoke/instrumental-versions).
# Banned on name alone.
_COVER_PRODUCT_RE = re.compile(
    r"\b(?:rockabye|renditions?\s+of|lullaby\s+renditions?|tribute|karaoke"
    r"|made\s+famous|in\s+the\s+style\s+of|8-?bit\s+versions?|piano\s+tribute)\b",
    re.I)
# HITS / COLLECTION / ANTHOLOGY repackagings — fake ONLY when Spotify itself
# marks the album a 'compilation'. This is the guard that saves real studio
# albums that merely use the word: "TTPD: THE ANTHOLOGY" (album), "Life Hits
# You Harder" (album), Lauv "...(the playlist)" (album). Word-boundaried so
# "Recollection" / "Hitsville" aren't caught.
_HITS_COMP_RE = re.compile(
    r"\b(?:greatest\s+hits|the\s+hits|best\s+of|the\s+very\s+best|number\s+ones?"
    r"|hits\b|collection\b|discograph\w*|anthology|compilation|essentials?\b"
    r"|singles\s+collection|the\s+singles\b|b-?sides\b)\b",
    re.I)


def is_cover_product_name(name: str) -> bool:
    """Cover/tribute/lullaby product — never a real album."""
    return bool(_COVER_PRODUCT_RE.search(name or ""))


def is_fake_album_name(name: str, album_type=None) -> bool:
    """True for derivative/non-real albums the user never wants built.

    Cover products are always fake. Hits/collection/anthology names are fake
    only when Spotify marks the album a 'compilation' — so real studio albums
    that merely use the word survive. When album_type is unknown (None), the
    hits/collection class is treated as NOT fake (conservative: never dismantle
    a real album on a name guess)."""
    if is_cover_product_name(name):
        return True
    if _HITS_COMP_RE.search(name or ""):
        return str(album_type or "").lower() == "compilation"
    return False


# Alternate-version markers. When a liked song's OWN title has none of these, we must not
# acquire it from a remix/sped-up/NNNHz/instrumental release — the user wants the original.
_VERSION_RE = re.compile(
    r"\bre-?mix(?:es|ed)?\b|\bsped\s*up\b|\bslowed\b|\breverb\b|\bnightcore\b|\b8d\s*audio\b"
    r"|\binstrumental\b|\bkaraoke\b|\btribute\b|\bin the style of\b|\bmade famous\b"
    r"|\b\d{3}\s*hz\b",
    re.I)
# Deliberately NOT matched: bare "mix" ("2021 Mix", "Mono Mix", "Stereo Mix" are album
# editions, not remixes) and "cover(s)" (a covers album is a real album).


def _is_alt_version(name: str) -> bool:
    return bool(_VERSION_RE.search(name or ""))
from datetime import datetime

from sqlalchemy import select, func, delete

from .db import (
    session_scope, get_config,
    SpotifyLikedTrack, SpotifyArtistSync, SpotifyAlbum, SpotifyAlbumTrack,
)
from . import auth_spotify, spotify_client

log = logging.getLogger(__name__)

SEARCH_LIMIT = 10  # Spotify caps /search at 10 now


def _norm(s: str) -> str:
    s = unicodedata.normalize('NFKD', s or '')
    s = ''.join(ch for ch in s if not unicodedata.combining(ch))
    return ' '.join(s.lower().split())


# Soundtracks / scores / cast recordings are real, cohesive albums even when credited
# "Various Artists" — a song from one genuinely belongs there. They must NOT be treated
# like a generic 50-artist hits compilation.
_SOUNDTRACK_KW = (
    "soundtrack", "motion picture", "original score", "original cast", "cast recording",
    "music from", "songs from", "the musical", "broadway", "original television",
    "ost", "score)", "from the motion picture", "from the series", "original motion picture",
)


def _is_soundtrack_name(name: str) -> bool:
    n = (name or "").lower()
    return any(k in n for k in _SOUNDTRACK_KW)


def _mb_secondary(s, album_artist: str, name: str) -> str | None:
    """('soundtrack' | 'compilation' | None) from the MusicBrainz mirror (MbAlbumMeta) —
    the authoritative source that tells a film SOUNDTRACK apart from a generic hits
    COMPILATION even when the album title gives nothing away (e.g. 'Guardians of the
    Galaxy: Awesome Mix Vol. 1'). Matched on normalized album name (artist-key preferred)."""
    try:
        from .autofill_engine import _normalize_for_key as _nk
        from .db import MbAlbumMeta
        nk = _nk(name or "")
        if not nk:
            return None
        rows = s.scalars(select(MbAlbumMeta).where(MbAlbumMeta.album_key == nk)).all()
        if not rows:
            return None
        ak = _nk(album_artist or "")
        row = next((r for r in rows if r.artist_key == ak), rows[0])
        sec = (row.secondary_types or "").lower()
        if "soundtrack" in sec:
            return "soundtrack"
        if "compilation" in sec:
            return "compilation"
    except Exception:
        pass
    return None


def _is_various_artist_comp(al, s=None) -> bool:
    """True only for a GENERIC multi-artist compilation (the 'NOW That's What I Call…' /
    'Greatest Hits of the 80s' kind). Credited Various Artists AND not a soundtrack.
    Single-artist albums (even 'Greatest Hits') and Various-Artists SOUNDTRACKS / scores /
    cast recordings are REAL albums → return False so they stay valid acquisition targets.
    Soundtrack detection: album-title keywords first, then MusicBrainz secondary-types."""
    if is_blocked_comp_name(al.name):
        return True                        # hard-blocked hits series (Now That's What I Call…)
    aa = (al.album_artist or "").strip().lower()
    is_va = ("various artist" in aa) or (aa in ("va", "v.a.", "various", "varios artistas"))
    if not is_va:
        return False                       # single-artist → always a real album
    if _is_soundtrack_name(al.name):
        return False                       # titled soundtrack/score/cast → real
    if s is not None and _mb_secondary(s, al.album_artist, al.name) == "soundtrack":
        return False                       # MusicBrainz confirms it's a soundtrack → real
    return True                            # Various Artists + no soundtrack signal → generic comp


# ── seeding ───────────────────────────────────────────────────────────────────

def seed_artist_sync() -> int:
    """Populate spotify_artist_sync from the liked-songs mirror (one row per artist).
    Idempotent — only adds artists not already tracked. Returns count added."""
    added = 0
    with session_scope() as s:
        rows = s.execute(
            select(SpotifyLikedTrack.artist,
                   func.max(SpotifyLikedTrack.primary_artist_id),
                   func.count())
            .group_by(SpotifyLikedTrack.artist)
        ).all()
        existing = {r.artist_key for r in s.scalars(select(SpotifyArtistSync)).all()}
        for artist, pid, cnt in rows:
            ak = _norm(artist)
            if not ak or ak in existing:
                continue
            s.add(SpotifyArtistSync(
                artist_key=ak, artist_name=artist, spotify_artist_id=pid,
                liked_count=int(cnt or 0), status="pending",
            ))
            existing.add(ak)
            added += 1
    if added:
        log.info("catalog: seeded %d artists for sync", added)
    return added


# ── background sync tick ────────────────────────────────────────────────────────

def spotify_catalog_sync_tick(max_artists: int | None = None) -> dict:
    """Sync the next few pending artists' catalogs into the mirror. Gentle + resumable.
    Highest liked_count artists first (consolidation matters most for them)."""
    out = {"processed": 0, "albums": 0, "tracks": 0, "remaining": 0, "errors": 0}
    # one-time/idempotent seed in case liked songs grew since last seed
    try:
        seed_artist_sync()
    except Exception:
        log.exception("catalog: seed during tick failed (continuing)")

    if max_artists is None:
        try:
            max_artists = int(get_config("catalog_sync_max_artists", "2") or "2")
        except Exception:
            max_artists = 2

    sp = auth_spotify.get_client()
    if not sp:
        out["error"] = "no spotify client"
        return out

    with session_scope() as s:
        out["remaining"] = s.scalar(
            select(func.count()).select_from(SpotifyArtistSync)
            .where(SpotifyArtistSync.status == "pending")
        ) or 0
        pend = list(s.scalars(
            select(SpotifyArtistSync)
            .where(SpotifyArtistSync.status == "pending")
            .order_by(SpotifyArtistSync.liked_count.desc())
            .limit(max_artists)
        ).all())
        # detach the bits we need (avoid lazy-load after session closes)
        pend = [(r.artist_key, r.artist_name, r.spotify_artist_id) for r in pend]

    for artist_key, artist_name, pid in pend:
        try:
            n_alb, n_trk = _sync_one_artist(sp, artist_name, pid)
            with session_scope() as s:
                r = s.get(SpotifyArtistSync, artist_key)
                if r:
                    r.status = "done"
                    r.albums_synced = n_alb
                    r.last_synced_at = datetime.utcnow()
                    r.error = None
            out["processed"] += 1
            out["albums"] += n_alb
            out["tracks"] += n_trk
        except Exception as e:
            out["errors"] += 1
            log.exception("catalog: sync failed for %s", artist_name)
            with session_scope() as s:
                r = s.get(SpotifyArtistSync, artist_key)
                if r:
                    r.status = "error"
                    r.error = str(e)[:300]
    if out["processed"] or out["errors"]:
        log.info("catalog: tick processed=%d albums=%d tracks=%d errors=%d remaining=%d",
                 out["processed"], out["albums"], out["tracks"], out["errors"], out["remaining"])
    return out


def _resolve_artist_id(sp, name: str, pid: str | None) -> str | None:
    if pid:
        return pid
    try:
        r = spotify_client._retry_patient(sp.search, q='artist:"%s"' % name,
                                          type="artist", limit=SEARCH_LIMIT)
    except Exception:
        return None
    items = ((r or {}).get("artists") or {}).get("items") or []
    return items[0]["id"] if items else None


def _sync_one_artist(sp, artist_name: str, pid: str | None) -> tuple[int, int]:
    aid = _resolve_artist_id(sp, artist_name, pid)
    if not aid:
        return 0, 0
    seen, albums = set(), []
    # Spotify caps /artists/{id}/albums at limit<=10 (same clampdown as /search).
    # Skip 'single' — singles are 1-track, useless for consolidation, and would
    # multiply the call budget. 'album' + 'compilation' is what the planner needs.
    for grp in ("album", "compilation"):
        offset = 0
        while True:
            r = spotify_client._retry_patient(sp.artist_albums, aid, album_type=grp,
                                              country="US", limit=10, offset=offset)
            items = (r or {}).get("items") or []
            for a in items:
                if a.get("id") and a["id"] not in seen:
                    seen.add(a["id"])
                    albums.append(a)
            if len(items) < 10:
                break
            offset += 10
    n_alb = n_trk = 0
    for a in albums:
        try:
            n_trk += _store_album(sp, a)
            n_alb += 1
        except Exception:
            log.exception("catalog: store album failed for %s", a.get("id"))
    return n_alb, n_trk


def _store_album(sp, a: dict) -> int:
    aid = a["id"]
    art = (a.get("artists") or [{}])[0].get("name", "")
    with session_scope() as s:
        s.merge(SpotifyAlbum(
            album_id=aid, name=a.get("name", ""),
            album_type=a.get("album_type", "album"),
            album_artist=art, album_artist_key=_norm(art),
            total_tracks=a.get("total_tracks", 0) or 0,
            release_date=(a.get("release_date") or "")[:16],
            image_url=((a.get("images") or [{}])[0].get("url") or "")[:512],
            fetched_at=datetime.utcnow(),
        ))
        # idempotent: clear this album's tracks, re-insert fresh
        s.execute(delete(SpotifyAlbumTrack).where(SpotifyAlbumTrack.album_id == aid))

    n = 0
    offset = 0
    while True:
        tr = spotify_client._retry_patient(sp.album_tracks, aid, limit=50, offset=offset, market="US")
        items = (tr or {}).get("items") or []
        # NOTE: this Spotify app has restricted API access — /v1/tracks returns 403
        # Forbidden, and /albums/{id}/tracks returns SIMPLIFIED tracks (no ISRC). So the
        # mirror matches liked songs by (artist + normalized title), not ISRC.
        with session_scope() as s:
            for it in items:
                if not it.get("id"):
                    continue
                s.add(SpotifyAlbumTrack(
                    album_id=aid, position=it.get("track_number", 0) or 0,
                    track_id=it["id"], isrc=None,
                    title=it.get("name", ""), title_key=_norm(it.get("name", "")),
                ))
                n += 1
        if len(items) < 50:
            break
        offset += 50
    return n


# ── read accessors (used by the picker + planner — NO Spotify) ──────────────────

def best_album_for_liked(track_id: str) -> dict | None:
    """From the mirror only: the album to acquire a liked track from (matched by the
    liked track's ISRC, falling back to normalized title within the same artist).
    Prefers the BIGGEST album (most total_tracks), with album_type='album' only as a
    tiebreak — so a song always lands in its largest album, matching the dedup sweep's
    keep-rule and avoiding the same song being fetched onto a smaller release later.
    Returns dict or None."""
    with session_scope() as s:
        lt = s.get(SpotifyLikedTrack, track_id)
        if not lt:
            return None
        from .version_match import paren_demo_live as _pdl
        _liked_is_alt = _is_alt_version(lt.title)
        _liked_dl = _pdl(lt.title)
        cand_album_ids: set[str] = set()
        if lt.isrc:
            cand_album_ids |= {
                r.album_id for r in s.scalars(
                    select(SpotifyAlbumTrack).where(SpotifyAlbumTrack.isrc == lt.isrc)
                ).all()
            }
        if not cand_album_ids:
            tk = _norm(lt.title)
            ak = _norm(lt.artist)
            for r in s.scalars(select(SpotifyAlbumTrack).where(SpotifyAlbumTrack.title_key == tk)).all():
                al = s.get(SpotifyAlbum, r.album_id)
                if al and al.album_artist_key == ak:
                    cand_album_ids.add(r.album_id)
        if not cand_album_ids:
            return None
        best = None
        bestkey = None
        for aid in cand_album_ids:
            al = s.get(SpotifyAlbum, aid)
            if not al:
                continue
            # RULE 4: an album is never created from a counterpart of more than
            # 50 songs — a giant box set can't be the acquisition target. The
            # liked song lands in its biggest <=50 album instead (or a single).
            if (al.total_tracks or 0) > 50:
                continue
            # A real album outranks a generic multi-artist hits compilation no matter how big
            # the comp is — BUT soundtracks / scores / cast recordings are real albums even
            # when credited "Various Artists", so they must still win. So the ONLY thing we
            # push to last-resort is a Various-Artists album that is NOT a soundtrack. Then:
            # biggest, then album_type='album' as a final tiebreak.
            is_generic_comp = _is_various_artist_comp(al, s)
            # Alternate-version penalty: a remix/sped-up/NNNHz release ranks below a clean
            # release UNLESS the liked song itself is that version (then it's what we want).
            is_alt = (_is_alt_version(al.name) or _pdl(al.name)) and not (_liked_is_alt or _liked_dl)
            is_fake = is_fake_album_name(al.name, getattr(al, "album_type", None))
            key = (0 if is_generic_comp else 1, 0 if is_fake else 1, 0 if is_alt else 1,
                   al.total_tracks or 0, 1 if al.album_type == "album" else 0)
            if bestkey is None or key > bestkey:
                best, bestkey, best_al = aid, key, al
        if best is None:
            return None
        if _is_alt_version(best_al.name) and not _liked_is_alt:
            # The only album the mirror has is an alternate version (e.g. a remix) but the
            # liked song is the original — don't acquire it; let the picker grab the original
            # single track instead.
            log.info("best_album_for_liked: skipping alt-version album %r for original track %s",
                     best_al.name, track_id)
            return None
        if is_fake_album_name(best_al.name, getattr(best_al, "album_type", None)):
            # The only album the mirror has for this liked song is a derivative
            # package (greatest hits / collection / lullaby / tribute / karaoke).
            # Never build it — let the picker grab the original single track.
            log.info("best_album_for_liked: skipping fake/derivative album %r for track %s",
                     best_al.name, track_id)
            return None
        if _is_various_artist_comp(best_al, s):
            # The only album the mirror has for this song is a generic hits compilation
            # (e.g. "Now That's What I Call Music!"). Never acquire/create it — return None so
            # the picker falls back to a single-track grab instead of building the comp folder.
            log.info("best_album_for_liked: skipping generic compilation %r for track %s",
                     best_al.name, track_id)
            return None
        tids = [
            t.track_id for t in s.scalars(
                select(SpotifyAlbumTrack).where(SpotifyAlbumTrack.album_id == best)
                .order_by(SpotifyAlbumTrack.position)
            ).all()
        ]
        return {
            "album_id": best,
            "album_url": f"https://open.spotify.com/album/{best}",
            "name": best_al.name,
            "total_tracks": best_al.total_tracks or 0,
            "track_ids": tids,
        }


def liked_title(track_id: str) -> str | None:
    """The cached title of a liked track (for Soulseek query quality) — no Spotify."""
    with session_scope() as s:
        lt = s.get(SpotifyLikedTrack, track_id)
        return lt.title if lt else None
