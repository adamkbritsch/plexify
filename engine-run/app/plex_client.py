"""Plex Music client — destination-only.

Reads from + writes to a user's Plex Music library. Never invoked from Spotify
paths directly; only from `mirror_from_local` which reads the local backup.

Key behaviors (after the 20-bug audit):
- Search filter sanitizes Plex's metacharacters (`()*[]`) so weird titles don't
  break the query parser.
- `add_tracks` fetches items individually with per-item try/except so a single
  stale Plex track ID doesn't kill the whole batch.
- `remove_tracks` batches into a single `removeItems` call.
- `create_playlist` deduplicates by name — re-uses an existing playlist of the
  same name instead of creating a second one.
- `_is_lossless` checks ALL media variants (handles FLAC at media[1+]).
"""
import logging
import re
import time
import threading
from dataclasses import dataclass
from typing import Iterable, Optional

from plexapi.exceptions import NotFound, PlexApiException
from plexapi.server import PlexServer

from .db import get_config

log = logging.getLogger(__name__)

LOSSLESS_CODECS = {"flac", "alac"}

# In-process cache for the dashboard health check.
# C4: lock against gunicorn-thread races.
# Q30: cache value keyed by (url, token) so changing creds doesn't return
# a stale "connected: True" for up to 30s.
_HEALTH_CACHE: dict = {"key": None, "value": None, "ts": 0.0}
_HEALTH_TTL_S = 30.0
_HEALTH_LOCK = threading.Lock()

# H10: cached PlexServer instance, keyed by (url, token). Avoids the network
# handshake on every call to _connect (which happens N times per page render).
_SERVER_CACHE: dict = {"key": None, "server": None, "ts": 0.0}
_SERVER_TTL_S = 300.0
_SERVER_LOCK = threading.Lock()

# Strip Plex filter metacharacters (parens, brackets, asterisks, etc.) that
# break the server's filter-string parser when interpolated into titles.
_PLEX_FILTER_META = re.compile(r"[()\[\]*?]")


@dataclass
class Track:
    key: str
    title: str
    artist: str
    album: Optional[str]
    duration_ms: int
    codec: str
    bitrate: Optional[int]
    isrc: Optional[str] = None


@dataclass
class Playlist:
    key: str
    name: str
    track_count: int


def _connect() -> Optional[PlexServer]:
    """H10: returns a cached PlexServer when creds unchanged; otherwise
    handshakes once and caches for up to _SERVER_TTL_S seconds."""
    url = get_config("plex_url")
    token = get_config("plex_token")
    if not (url and token):
        return None
    url = url.rstrip("/")
    key = (url, token)
    now = time.time()
    with _SERVER_LOCK:
        if (_SERVER_CACHE["key"] == key
                and _SERVER_CACHE["server"] is not None
                and (now - _SERVER_CACHE["ts"]) < _SERVER_TTL_S):
            return _SERVER_CACHE["server"]
    # Construct outside the lock so a slow handshake doesn't block other ops
    try:
        srv = PlexServer(url, token, timeout=15)
    except Exception as e:
        log.exception("Plex connect failed: %s", e)
        with _SERVER_LOCK:
            _SERVER_CACHE["key"] = None
            _SERVER_CACHE["server"] = None
            _SERVER_CACHE["ts"] = 0.0
        return None
    with _SERVER_LOCK:
        _SERVER_CACHE["key"] = key
        _SERVER_CACHE["server"] = srv
        _SERVER_CACHE["ts"] = now
    return srv


def is_authed() -> bool:
    return _connect() is not None


def _music_section(plex: PlexServer):
    section_key = get_config("plex_music_section_key")
    try:
        if section_key:
            return plex.library.sectionByID(int(section_key))
        for s in plex.library.sections():
            if getattr(s, "type", None) == "artist":
                return s
    except Exception as e:
        log.exception("Plex music section lookup failed: %s", e)
    return None


def list_music_sections() -> list[dict]:
    plex = _connect()
    if not plex:
        return []
    out = []
    for s in plex.library.sections():
        if getattr(s, "type", None) == "artist":
            out.append({"key": str(s.key), "title": s.title})
    return out


# ── audiobooks ────────────────────────────────────────────────────────────────
# The Audiobooks library is ALSO type "artist" (a Plex Music library with the Audnexus agent) —
# so unlike _music_section there is deliberately NO type-based fallback here: with two artist
# sections in the server a guess could point audiobook automation at the music library.

def _audiobook_section(plex: PlexServer):
    section_key = get_config("plex_audiobook_section_key")
    if not section_key:
        return None
    try:
        return plex.library.sectionByID(int(section_key))
    except Exception as e:
        log.exception("Plex audiobook section lookup failed: %s", e)
    return None


def list_audiobook_sections() -> list[dict]:
    """All artist-type sections — the settings picker chooses which one is Audiobooks."""
    return list_music_sections()


def trigger_audiobook_scan() -> bool:
    plex = _connect()
    sec = _audiobook_section(plex) if plex else None
    if not sec:
        return False
    try:
        sec.update()
        return True
    except Exception:
        log.exception("audiobook section scan trigger failed")
        return False


def reconcile_audiobook_albums() -> dict:
    """Heal multi-part split damage in the audiobook section: parts arriving across separate
    scans can split one book into several albums (per-part agent titles, sometimes under a
    duplicate local:// artist). Snapshot the section, let the organizer's pure planner decide,
    apply: merge split albums, title multi-part albums after their book folder (locked), restore
    part track numbers (locked). No-op when nothing is split — safe to run from the tick."""
    import os
    from urllib.parse import urlencode
    from . import audiobook_organizer

    out = {"merged": 0, "retitled": 0, "reindexed": 0}
    plex = _connect()
    sec = _audiobook_section(plex) if plex else None
    if not sec:
        return out
    try:
        snapshot = []
        for alb in sec.albums():
            tracks, dirs = [], {}
            for t in alb.tracks():
                f = ""
                try:
                    f = t.media[0].parts[0].file or ""
                except (IndexError, AttributeError):
                    pass
                if f:
                    d = os.path.dirname(f)
                    dirs[d] = dirs.get(d, 0) + 1
                tracks.append({"key": t.ratingKey, "index": t.trackNumber, "file": f})
            snapshot.append({
                "key": alb.ratingKey,
                "title": alb.title or "",
                "dir": max(dirs, key=dirs.get) if dirs else "",
                "agent_matched": not str(getattr(alb, "guid", "") or "").startswith("local://"),
                "tracks": tracks,
            })
        plan = audiobook_organizer.plan_plex_reconcile(snapshot)
        for primary, others in plan["merges"]:
            plex.query(f"/library/metadata/{primary}/merge?ids={','.join(str(k) for k in others)}",
                       method=plex._session.put)
            out["merged"] += len(others)
        for key, title in plan["retitles"]:
            q = urlencode({"type": 9, "title.value": title, "title.locked": 1,
                           "titleSort.value": title, "titleSort.locked": 1})
            plex.query(f"/library/metadata/{key}?{q}", method=plex._session.put)
            out["retitled"] += 1
        for key, idx in plan["reindexes"]:
            q = urlencode({"type": 10, "index.value": idx, "index.locked": 1})
            plex.query(f"/library/metadata/{key}?{q}", method=plex._session.put)
            out["reindexed"] += 1
        for key in plan.get("refreshes", []):
            # '[Unknown Album]' = a scan caught the file mid-copy; its tags exist now, so a
            # metadata refresh repairs the title. NEVER a delete — with allowMediaDeletion
            # on, DELETE /library/metadata removes the FILES (2026-07-10 incident).
            plex.query(f"/library/metadata/{key}/refresh", method=plex._session.put)
            out["refreshed"] = out.get("refreshed", 0) + 1
    except Exception:
        log.exception("reconcile_audiobook_albums failed")
    return out


def list_audiobook_albums() -> list:
    """[{key, title, author, rel_dir, tracks}] for every album in the audiobook section.
    rel_dir is the book folder RELATIVE to the section's library location — exactly what the
    daemon's soft-delete endpoint takes, so no host-path mapping ever happens client-side."""
    import os
    plex = _connect()
    sec = _audiobook_section(plex) if plex else None
    if not sec:
        return []
    locs = []
    try:
        locs = [l.rstrip("/") for l in (sec.locations or []) if l]
    except Exception:
        pass
    out = []
    try:
        for alb in sec.albums():
            rel_dir, n = "", 0
            try:
                tracks = alb.tracks()
                n = len(tracks)
            except Exception:
                tracks = []
            for t in tracks:
                # per-track guard: one partless/stale track must not blank the whole album
                try:
                    f = t.media[0].parts[0].file or ""
                except (IndexError, AttributeError):
                    continue
                for l in locs:
                    if f.startswith(l + "/"):
                        rel_dir = os.path.dirname(f)[len(l):].strip("/")
                        break
                if rel_dir:
                    break
            added = 0
            try:
                added = int(alb.addedAt.timestamp()) if getattr(alb, "addedAt", None) else 0
            except Exception:
                pass
            out.append({"key": alb.ratingKey, "title": alb.title or "",
                        "author": getattr(alb, "parentTitle", "") or "", "rel_dir": rel_dir,
                        "tracks": n, "thumb": getattr(alb, "thumb", None) or "",
                        "added_at": added})
    except Exception:
        log.exception("list_audiobook_albums failed")
    return out


def cleanup_deleted_album(rel_dir: str) -> dict:
    """After a book's files moved to trash: rescan the section and empty Plex's own trash
    flags — verified live (2026-07-10) to fully remove the album; the Music scanner DOES flag
    missing albums. Deliberately NEVER calls DELETE /library/metadata: with media deletion
    enabled that endpoint destroys files (it already destroyed two books today), and this code
    path must stay incapable of that under ANY server settings.

    Two review-driven guards: the scan is POLLED to completion (a fixed sleep raced slow
    scans and the emptyTrash no-opped), and emptyTrash is SKIPPED when the scan made several
    OTHER albums vanish too — that pattern means the mount was flaky mid-scan, and a blanket
    emptyTrash would purge healthy books' metadata (positions, added-dates). A missed window
    is retried by audiobook_tick, so returning cleared=False is safe."""
    out = {"scanned": False, "cleared": False}
    plex = _connect()
    sec = _audiobook_section(plex) if plex else None
    if not sec or not rel_dir:
        return out
    try:
        before = {a["rel_dir"] for a in list_audiobook_albums() if a["rel_dir"]}
        sec.update()
        out["scanned"] = True
        deadline = time.time() + 90
        while time.time() < deadline:
            time.sleep(5)
            try:
                sec.reload()
                if not getattr(sec, "refreshing", False):
                    break
            except Exception:
                break
        after = {a["rel_dir"] for a in list_audiobook_albums() if a["rel_dir"]}
        others_gone = before - after - {rel_dir}
        if len(others_gone) > 2:
            log.warning("skipping emptyTrash: %d unrelated albums vanished during the scan "
                        "(flaky mount?) — retrying later instead of purging their metadata",
                        len(others_gone))
            return out
        try:
            sec.emptyTrash()
        except Exception:
            pass
        time.sleep(5)
        lingering = [a for a in list_audiobook_albums() if a["rel_dir"] == rel_dir]
        out["cleared"] = not lingering
        if lingering:
            log.warning("deleted book still listed in Plex (%s) — audiobook_tick retries; "
                        "NEVER metadata-deleted by design", rel_dir)
    except Exception:
        log.exception("cleanup_deleted_album failed")
    return out


_COVER_CACHE: dict = {}


def audiobook_cover(key: int) -> Optional[bytes]:
    """Album art bytes for the app's shelf grid — proxied so the Plex token never leaves the
    engine. Cached per key for an hour (covers change only on re-match)."""
    now = time.time()
    hit = _COVER_CACHE.get(key)
    if hit and now - hit[0] < 3600:
        return hit[1]
    plex = _connect()
    if not plex:
        return None
    try:
        import requests as _rq
        from urllib.parse import quote
        # grid-sized transcode — the originals are 2400x2400 (~0.5MB each; a 60-book shelf
        # would pull 30MB+)
        inner = quote(f"/library/metadata/{int(key)}/thumb", safe="")
        url = (f"{plex._baseurl}/photo/:/transcode?width=320&height=320"
               f"&minSize=1&upscale=1&url={inner}")
        r = _rq.get(url, headers={"X-Plex-Token": plex._token}, timeout=10)
        if r.status_code == 200 and r.content:
            if len(_COVER_CACHE) > 300:
                _COVER_CACHE.clear()
            _COVER_CACHE[key] = (now, r.content)
            return r.content
    except Exception:
        log.warning("audiobook cover fetch failed for %s", key)
    return None


def create_audiobook_section(location: str, name: str = "Audiobooks") -> dict:
    """Create the Audiobooks library (Music type + Audnexus agent + Plex Music Scanner).
    Requires the Audnexus.bundle agent to already be installed and Plex restarted — otherwise
    Plex rejects the unknown agent. Returns {ok, key} or {ok: False, error}."""
    plex = _connect()
    if not plex:
        return {"ok": False, "error": "Plex not connected (check plex_url / plex_token)"}
    try:
        existing = {s.title.lower() for s in plex.library.sections()}
        if name.lower() in existing:
            sec = next(s for s in plex.library.sections() if s.title.lower() == name.lower())
            return {"ok": True, "key": str(sec.key), "existed": True}
        plex.library.add(name=name, type="artist",
                         agent="com.plexapp.agents.audnexus",
                         scanner="Plex Music Scanner",
                         language="en", location=location)
        # library.add returns None on some plexapi versions — re-find the section
        plex.library.reload()
        sec = next((s for s in plex.library.sections()
                    if s.title.lower() == name.lower()), None)
        if not sec:
            return {"ok": False, "error": "created but section not found on reload"}
        try:
            # Best-effort advanced prefs; agent ORDER and some prefs are UI-only (documented
            # manual checklist in docs/AUDIOBOOKS.md).
            sec.editAdvanced(respectTags=0)
        except Exception:
            pass
        return {"ok": True, "key": str(sec.key)}
    except Exception as e:
        log.exception("create_audiobook_section failed")
        return {"ok": False, "error": str(e)[:300]}


def _is_lossless(track) -> tuple[bool, str, Optional[int]]:
    """Inspect a plexapi Track for any FLAC/ALAC media variant.

    Iterates ALL media (not just media[0]) because Plex tracks can have
    multiple file copies and FLAC might be at index 1+.
    """
    best_codec = ""
    best_bitrate = None
    is_lossless = False
    try:
        media = getattr(track, "media", None) or []
        for m in media:
            codec = (getattr(m, "audioCodec", "") or "").lower()
            br = getattr(m, "bitrate", None)
            if codec in LOSSLESS_CODECS:
                is_lossless = True
                if best_bitrate is None or (br and best_bitrate is not None and br > best_bitrate):
                    best_codec = codec
                    best_bitrate = br
                elif best_bitrate is None:
                    best_codec = codec
                    best_bitrate = br
            elif not is_lossless:
                best_codec = codec or best_codec
                if best_bitrate is None:
                    best_bitrate = br
    except Exception:
        pass
    return (is_lossless, best_codec, best_bitrate)


def _to_track(t) -> Track:
    # Bug #13 fix: prefer originalTitle (the track-level artist) over
    # grandparentTitle (the album artist). The latter shows "Various Artists"
    # on compilations, which destroys our matcher's artist comparison.
    artist = (getattr(t, "originalTitle", None)
              or getattr(t, "grandparentTitle", None) or "")
    album = getattr(t, "parentTitle", None)
    duration_ms = int(getattr(t, "duration", 0) or 0)
    _, codec, bitrate = _is_lossless(t)
    return Track(
        key=str(t.ratingKey),
        title=getattr(t, "title", "") or "",
        artist=str(artist),
        album=album,
        duration_ms=duration_ms,
        codec=codec,
        bitrate=bitrate,
    )


def list_playlists() -> list[Playlist]:
    plex = _connect()
    if not plex:
        return []
    out = []
    try:
        for p in plex.playlists(playlistType="audio"):
            try:
                out.append(Playlist(key=str(p.ratingKey), name=p.title, track_count=p.leafCount or 0))
            except Exception:
                continue
    except Exception as e:
        log.exception("Plex list playlists failed: %s", e)
    return out


def find_playlist_by_name(name: str) -> Optional[str]:
    """Return ratingKey of an existing audio playlist with this exact name, else None.

    Used by create_playlist to dedupe — prevents duplicate playlists when the
    app re-creates a pair.
    """
    plex = _connect()
    if not plex:
        return None
    try:
        for p in plex.playlists(playlistType="audio"):
            if (p.title or "").strip() == name.strip():
                return str(p.ratingKey)
    except Exception as e:
        log.debug("find_playlist_by_name(%r) failed: %s", name, e)
    return None


def get_playlist_tracks(playlist_key: str) -> list[Track]:
    plex = _connect()
    if not plex:
        return []
    try:
        pl = plex.fetchItem(int(playlist_key))
    except NotFound:
        log.warning("Plex playlist %s not found (was it deleted?)", playlist_key)
        raise
    except Exception as e:
        log.exception("Plex fetch playlist failed: %s", e)
        return []
    out = []
    try:
        for t in pl.items():
            try:
                out.append(_to_track(t))
            except Exception:
                continue
    except Exception as e:
        log.exception("Plex playlist items failed: %s", e)
    return out


def create_playlist(name: str, first_track_keys: list[str]) -> Optional[str]:
    """Create or reuse a Plex audio playlist with the given name.

    Bug #10 fix: if a playlist with this name already exists, reuse it instead
    of creating a duplicate. Useful when the user re-creates a pair and the
    previous Plex playlist still exists.
    """
    plex = _connect()
    if not plex or not first_track_keys:
        return None
    # Dedupe by name
    existing = find_playlist_by_name(name)
    if existing:
        log.info("create_playlist: reusing existing Plex playlist %s named %r", existing, name)
        return existing
    try:
        items = []
        for k in first_track_keys:
            try:
                items.append(plex.fetchItem(int(k)))
            except NotFound:
                log.warning("create_playlist: seed track %s not in Plex anymore; skipping", k)
                continue
        if not items:
            return None
        pl = plex.createPlaylist(title=name, items=items)
        return str(pl.ratingKey)
    except PlexApiException as e:
        log.exception("Plex create_playlist failed: %s", e)
        return None


PLEX_ADD_CHUNK = 100
PLEX_OP_DELAY_S = 0.2


def add_tracks(playlist_key: str, track_keys: Iterable[str]) -> list[str]:
    """Add tracks to a Plex playlist in 100-track chunks. Returns the list of keys actually added.

    Internal chunking protects against Plex's per-request URL/payload limits
    when adding very large batches. Callers can pass any-sized list; this
    function calls Plex's `addItems` 100 at a time with a small inter-chunk
    delay to be polite under load.

    Bug fixes in this function:
    - Per-item fetch with NotFound rescue — one stale Plex ID no longer kills a batch.
    - Returns the actually-added keys (excludes stale).
    """
    plex = _connect()
    if not plex:
        return []
    keys = list(dict.fromkeys(track_keys))
    if not keys:
        return []
    try:
        pl = plex.fetchItem(int(playlist_key))
    except NotFound:
        log.error("add_tracks: playlist %s no longer exists", playlist_key)
        raise

    all_added: list[str] = []
    for ci in range(0, len(keys), PLEX_ADD_CHUNK):
        chunk = keys[ci:ci + PLEX_ADD_CHUNK]
        valid_items = []
        valid_keys = []
        for k in chunk:
            try:
                item = plex.fetchItem(int(k))
                valid_items.append(item)
                valid_keys.append(k)
            except NotFound:
                log.info("add_tracks: Plex track %s gone; skipping", k)
            except Exception as e:
                log.warning("add_tracks: fetchItem(%s) failed: %s", k, e)
        if not valid_items:
            continue
        try:
            pl.addItems(valid_items)
            all_added.extend(valid_keys)
        except Exception as e:
            log.exception("Plex addItems chunk failed (offset %d): %s", ci, e)
            # Stop here so caller can persist what was added; subsequent retry resumes.
            break
        if ci + PLEX_ADD_CHUNK < len(keys):
            time.sleep(PLEX_OP_DELAY_S)
    return all_added


def remove_tracks(playlist_key: str, track_keys: Iterable[str]) -> list[str]:
    """Remove tracks from a Plex playlist. Returns the list of keys actually removed.

    Bug #2 fix: batches all removals into a single removeItems call.
    Bug #3 fix: loads playlist items ONCE.
    """
    plex = _connect()
    if not plex:
        return []
    keys_set = {str(k) for k in track_keys}
    if not keys_set:
        return []
    try:
        pl = plex.fetchItem(int(playlist_key))
    except NotFound:
        log.warning("remove_tracks: playlist %s gone", playlist_key)
        return []
    try:
        items_to_remove = [t for t in pl.items() if str(t.ratingKey) in keys_set]
    except Exception as e:
        log.exception("Plex remove_tracks fetch items failed: %s", e)
        return []
    if not items_to_remove:
        return []
    try:
        pl.removeItems(items_to_remove)
        return [str(t.ratingKey) for t in items_to_remove]
    except Exception as e:
        log.exception("Plex removeItems failed: %s", e)
        return []


def validate_track(key: str) -> bool:
    """Bug #5 helper: check that a cached plex_track_key still exists in Plex."""
    plex = _connect()
    if not plex:
        return False
    try:
        plex.fetchItem(int(key))
        return True
    except NotFound:
        return False
    except Exception:
        # Be optimistic on transient errors; don't invalidate cache on network blip
        return True


def search_track(title: str, artist: str, duration_ms: Optional[int] = None) -> Optional[Track]:
    """Find a lossless (FLAC/ALAC) copy of a track in the Plex music library."""
    plex = _connect()
    if not plex:
        return None
    section = _music_section(plex)
    if not section:
        return None
    title_clean = (title or "").strip()
    artist_clean = (artist or "").strip()
    if not (title_clean and artist_clean):
        return None

    # Bug #7 fix: strip Plex filter metacharacters that break the query parser.
    title_for_filter = _PLEX_FILTER_META.sub("", title_clean)

    candidates: list[Track] = []
    seen_keys: set[str] = set()

    # VERSION-SUFFIX STRIP: Spotify titles carry " - Remastered 2009" /
    # " - 2021 Mix" / "(Live)" qualifiers that Plex track titles don't —
    # the #1 cause of downloaded-but-"not in Plex" false negatives
    # (e.g. every remastered Beatles song). Search both forms.
    import re as _re_v
    title_stripped = _re_v.sub(
        r"\s*[-–]\s*(remaster(ed)?|mono|stereo|live|acoustic|single|radio|"
        r"(19|20)\d{2}|mix|version|edit|take|rooftop)[^-–]*$",
        "", title_clean, flags=_re_v.IGNORECASE).strip()
    title_stripped = _re_v.sub(r"\s*\((remaster|mix|live|mono|stereo|version|edit|deluxe)[^)]*\)\s*$",
                               "", title_stripped, flags=_re_v.IGNORECASE).strip()
    # Attempt 1: title-filtered search (fast, precise)
    # Attempt 2: free-text fallback (broader, lets matcher filter by artist)
    attempts = [
        ("title-filter", lambda: section.searchTracks(filters={"track.title": title_for_filter}, limit=30)),
        ("free-text", lambda: section.search(title_clean, libtype="track", limit=30)),
    ]
    if title_stripped and title_stripped.lower() != title_clean.lower():
        _tsf = _PLEX_FILTER_META.sub("", title_stripped)
        attempts.append(("title-filter-stripped",
                         lambda: section.searchTracks(filters={"track.title": _tsf}, limit=30)))
        attempts.append(("free-text-stripped",
                         lambda: section.search(title_stripped, libtype="track", limit=30)))

    for label, fn in attempts:
        try:
            results = fn() or []
        except Exception as e:
            log.debug("Plex search (%s) for %r failed: %s", label, title_clean, e)
            continue
        for t in results:
            key = str(getattr(t, "ratingKey", "") or "")
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            from .version_match import demo_live_banned as _dlb
            if _dlb(getattr(t, "title", "") or "", title):
                continue  # (Demo)/(Live) take never counts as the clean liked song
            is_lossless, codec, _ = _is_lossless(t)
            if not is_lossless:
                continue
            try:
                candidates.append(_to_track(t))
            except Exception:
                continue
        if candidates:
            from .matcher import pick_best
            hit = pick_best(title, artist, duration_ms, candidates)
            if hit:
                return hit

    return None


def check_health(use_cache: bool = True) -> dict:
    url = get_config("plex_url") or ""
    token = get_config("plex_token") or ""
    key = (url, token)
    now = time.time()
    if use_cache:
        with _HEALTH_LOCK:
            if (_HEALTH_CACHE["key"] == key
                    and _HEALTH_CACHE["value"] is not None
                    and (now - _HEALTH_CACHE["ts"]) < _HEALTH_TTL_S):
                return _HEALTH_CACHE["value"]
    out = {"connected": False, "music_section": None, "track_count": None, "error": None}
    if not (get_config("plex_url") and get_config("plex_token")):
        out["error"] = "no credentials"
        with _HEALTH_LOCK:
            _HEALTH_CACHE["key"] = key
            _HEALTH_CACHE["value"] = out
            _HEALTH_CACHE["ts"] = now
        return out
    plex = _connect()
    if not plex:
        out["error"] = "connect failed"
        with _HEALTH_LOCK:
            _HEALTH_CACHE["key"] = key
            _HEALTH_CACHE["value"] = out
            _HEALTH_CACHE["ts"] = now
        return out
    out["connected"] = True
    try:
        section = _music_section(plex)
        if section:
            out["music_section"] = section.title
            out["track_count"] = section.totalSize
    except Exception as e:
        out["error"] = str(e)
    with _HEALTH_LOCK:
        _HEALTH_CACHE["key"] = key
        _HEALTH_CACHE["value"] = out
        _HEALTH_CACHE["ts"] = now
    return out


def invalidate_health_cache() -> None:
    with _HEALTH_LOCK:
        _HEALTH_CACHE["key"] = None
        _HEALTH_CACHE["value"] = None
        _HEALTH_CACHE["ts"] = 0.0
    with _SERVER_LOCK:
        _SERVER_CACHE["key"] = None
        _SERVER_CACHE["server"] = None
        _SERVER_CACHE["ts"] = 0.0


def is_configured() -> bool:
    return bool(get_config("plex_url") and get_config("plex_token"))
