"""Spotify Web API client — playlists, tracks, and liked songs."""

import logging
import re
import time
import threading
from dataclasses import dataclass
from typing import Iterable, Optional

import requests
import spotipy

from . import auth_spotify

log = logging.getLogger(__name__)

_QUOTE_RE = re.compile(r'["\']')
_PAREN_RE = re.compile(r"\s*[\(\[].*?[\)\]]")
_FEAT_RE = re.compile(r"\s+\b(feat\.?|featuring|ft\.?)\b.*", re.IGNORECASE)



# Set by search_track when a 429 cut the search short — callers must treat that as "try
# again later", NOT as "this track does not exist on Spotify". Thread-LOCAL, not a module
# global: concurrent scheduler jobs each call search_track on their own thread, so a shared
# global lets one thread's reset clobber another's 429 signal before its caller reads it.
_search_state = threading.local()


def was_search_ratelimited() -> bool:
    """True if THIS thread's most recent search_track() was cut short by a 429."""
    return getattr(_search_state, "ratelimited", False)

def _sanitize(s: str) -> str:
    return _QUOTE_RE.sub("", s or "").strip()


def _strip_extras(s: str) -> str:
    s = _PAREN_RE.sub("", s or "")
    s = _FEAT_RE.sub("", s)
    return " ".join(s.split())


@dataclass
class Track:
    id: str
    title: str
    artist: str
    album: Optional[str]
    isrc: Optional[str]
    duration_ms: int


@dataclass
class Playlist:
    id: str
    name: str
    owner: str
    snapshot_id: str
    track_count: int
    owned: bool = True   # False = a playlist the user follows but doesn't own


def _retry(call, *args, _max_wait_s: float = 30.0, _max_attempts: int = 4,
           _on_wait=None, **kwargs):
    """Retry on transient errors. Patient by default for background work.

    Caller passes _max_wait_s (total sleep budget across retries) and _max_attempts.
    _on_wait is an optional callback `(seconds, attempt, reason) -> None` invoked
    before each sleep — useful for emitting job-progress events so the UI can
    show "waiting on Spotify rate-limit (60s)" instead of looking frozen.
    """
    last_err = None
    total_sleep = 0.0
    for attempt in range(_max_attempts):
        try:
            return call(*args, **kwargs)
        except spotipy.SpotifyException as e:
            last_err = e
            if e.http_status == 429:
                retry_after = int((e.headers or {}).get("Retry-After", "5"))
                sleep_for = retry_after + 1
                if total_sleep + sleep_for > _max_wait_s:
                    raise
                log.warning("Spotify 429 — sleeping %ss (attempt %d, budget %.0f/%.0fs)",
                            sleep_for, attempt, total_sleep, _max_wait_s)
                if _on_wait:
                    try: _on_wait(sleep_for, attempt, f"rate-limited (429), retry after {sleep_for}s")
                    except Exception: pass
                time.sleep(sleep_for)
                total_sleep += sleep_for
                continue
            if e.http_status and 500 <= e.http_status < 600:
                backoff = min(2 ** attempt, 30)
                if total_sleep + backoff > _max_wait_s:
                    raise
                if _on_wait:
                    try: _on_wait(backoff, attempt, f"Spotify {e.http_status}, backoff {backoff}s")
                    except Exception: pass
                time.sleep(backoff)
                total_sleep += backoff
                continue
            raise
        except (requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout,
                requests.exceptions.ChunkedEncodingError) as e:
            last_err = e
            backoff = min(2 ** attempt, 30)
            if total_sleep + backoff > _max_wait_s:
                raise
            log.warning("Spotify network error %s — backoff %ss", type(e).__name__, backoff)
            if _on_wait:
                try: _on_wait(backoff, attempt, f"network {type(e).__name__}, backoff {backoff}s")
                except Exception: pass
            time.sleep(backoff)
            total_sleep += backoff
            continue
    raise RuntimeError(f"Spotify call exhausted retries: {last_err}")


# Convenience wrappers for the two policies
def _retry_fast(call, *args, **kwargs):
    """For UI routes — fail in ~5s rather than block the user."""
    return _retry(call, *args, _max_wait_s=5.0, _max_attempts=2, **kwargs)


def _retry_patient(call, *args, **kwargs):
    """For background compile/sync — wait up to 1 hour total, 20 attempts."""
    return _retry(call, *args, _max_wait_s=3600.0, _max_attempts=20, **kwargs)


# In-process cache for the (cheap-but-not-free) list_my_playlists call.
# Watcher needs FRESH data per tick (sees use_cache=False); routes/dashboards
# tolerate 60s staleness.
# C4: bare dict mutated by gunicorn threads → race + torn reads. Lock now.
# C5: cache value keyed by current_user_id so account switches don't show stale.
_LIST_CACHE: dict = {"key": None, "value": None, "ts": 0.0}
_LIST_TTL_S = 60.0
_LIST_LOCK = threading.Lock()


def invalidate_list_cache() -> None:
    with _LIST_LOCK:
        _LIST_CACHE["key"] = None
        _LIST_CACHE["value"] = None
        _LIST_CACHE["ts"] = 0.0


def get_playlist_snapshot_id(playlist_id: str, *, patient: bool = True) -> Optional[str]:
    """One cheap API call: fetch only the snapshot_id for a playlist.

    Patient by default — used at the END of a compile to persist the snapshot,
    so we MUST eventually succeed or the watcher will re-trigger the compile
    next tick.
    """
    sp = auth_spotify.get_client()
    if not sp:
        return None
    retry = _retry_patient if patient else _retry_fast
    try:
        r = retry(sp.playlist, playlist_id, fields="snapshot_id")
        return (r or {}).get("snapshot_id")
    except Exception as e:
        log.warning("get_playlist_snapshot_id(%s) failed: %s", playlist_id, e)
        return None


def list_my_playlists(use_cache: bool = True, patient: bool = False) -> list[Playlist]:
    # C4: locked TTL cache; C5: keyed by current Spotify user id so account
    # switches don't return another user's playlists for up to 60s.
    sp = auth_spotify.get_client()
    if not sp:
        return []
    retry = _retry_patient if patient else _retry_fast

    # Q23: refuse to return playlists if we can't identify the current user —
    # the previous fallback ("return ALL playlists") could expose subscribed-
    # not-owned playlists to the mirror flow.
    my_id = None
    try:
        me = retry(sp.current_user)
        my_id = me.get("id") if me else None
    except Exception as e:
        log.warning("current_user() failed; returning empty list (no owner filter possible): %s", e)
        return []
    if not my_id:
        return []

    now = time.time()
    if use_cache:
        with _LIST_LOCK:
            if (_LIST_CACHE["key"] == my_id
                    and _LIST_CACHE["value"] is not None
                    and (now - _LIST_CACHE["ts"]) < _LIST_TTL_S):
                return _LIST_CACHE["value"]
    out: list[Playlist] = []
    page = retry(sp.current_user_playlists, limit=50)
    while page:
        for p in page.get("items") or []:
            if not p or not p.get("id"):
                continue
            owner = p.get("owner") or {}
            # ONLY owned playlists are listed: Spotify blocks third-party apps
            # from reading the items of any playlist the account doesn't own
            # (followed AND editorial), so anything else cannot compile and the
            # user asked that uncompilable playlists never be shown.
            if owner.get("id") != my_id:
                continue
            tracks_obj = p.get("tracks") if isinstance(p.get("tracks"), dict) else None
            count = -1
            if tracks_obj is not None:
                t = tracks_obj.get("total")
                if isinstance(t, int):
                    count = t
            try:
                out.append(Playlist(
                    id=p["id"],
                    name=p.get("name") or "(untitled)",
                    owner=owner.get("display_name") or owner.get("id") or "",
                    snapshot_id=p.get("snapshot_id") or "",
                    track_count=count,
                    owned=(owner.get("id") == my_id),
                ))
            except Exception as e:
                log.warning("Skipping malformed Spotify playlist %s: %s", p.get("id"), e)
                continue
        page = retry(sp.next, page) if page.get("next") else None
    with _LIST_LOCK:
        _LIST_CACHE["key"] = my_id
        _LIST_CACHE["value"] = out
        _LIST_CACHE["ts"] = now
    return out


class PlaylistNotFoundError(LookupError):
    """Spotify returned 404 — playlist was deleted or unfollowed."""


def fetch_playlist_page(
    playlist_id: str, *, offset: int = 0, limit: int = 100, patient: bool = True,
    on_wait=None,
) -> tuple[list[tuple[int, Track]], int, int, bool]:
    """Fetch a single page of a playlist's tracks.

    Returns (positioned_tracks, raw_item_count, total, has_next):
      - positioned_tracks: list of (absolute_position, Track) — position is the
        track's REAL index in the Spotify playlist (offset + raw item index),
        NOT a filtered-list index. This preserves source order even when local
        files / podcasts / removed tracks are skipped.
      - raw_item_count: number of items in this page (including filtered),
        used by the caller to advance offset correctly.
      - total: playlist track count per Spotify's response.
      - has_next: True if more pages.

    Local files, episodes, and tombstoned tracks (id missing or "null" string)
    are filtered. With patient=True, will wait up to an hour on 429s.
    """
    sp = auth_spotify.get_client()
    if not sp:
        raise RuntimeError("Spotify not authenticated")
    try:
        if patient:
            page = _retry(
                sp._get, f"playlists/{playlist_id}/items",
                _max_wait_s=3600.0, _max_attempts=20, _on_wait=on_wait,
                limit=limit, offset=offset, additional_types="track",
            )
        else:
            page = _retry_fast(
                sp._get, f"playlists/{playlist_id}/items",
                limit=limit, offset=offset, additional_types="track",
            )
    except spotipy.SpotifyException as e:
        if e.http_status == 403:
            raise PlaylistForbiddenError(
                f"Spotify 403 on playlist {playlist_id}/items: Spotify blocks third-party apps from reading "
                "playlists the account does not own (followed AND algorithmic/editorial)."
            ) from e
        if e.http_status == 404:
            raise PlaylistNotFoundError(
                f"Spotify 404 on playlist {playlist_id} — it was deleted or unfollowed."
            ) from e
        raise

    total = int(page.get("total") or 0)
    items = page.get("items") or []
    raw_item_count = len(items)
    positioned: list[tuple[int, Track]] = []
    for raw_idx, item in enumerate(items):
        if not item:
            continue
        t = item.get("track") or item.get("item")
        if not t:
            continue
        tid = t.get("id")
        # Tombstoned tracks come back as id=None or sometimes id="null" string
        if not tid or tid == "null" or t.get("is_local"):
            continue
        if t.get("type") and t.get("type") != "track":
            continue
        artists = ", ".join(a["name"] for a in (t.get("artists") or []) if a.get("name"))
        positioned.append((offset + raw_idx, Track(
            id=tid,
            title=t.get("name") or "",
            artist=artists,
            album=((t.get("album") or {}).get("name")),
            isrc=((t.get("external_ids") or {}).get("isrc")),
            duration_ms=int(t.get("duration_ms") or 0),
        )))
    has_next = bool(page.get("next"))
    return positioned, raw_item_count, total, has_next


class PlaylistForbiddenError(RuntimeError):
    """Spotify returned 403 for a playlist — typically algorithmic/editorial playlists."""


def get_playlist_tracks(playlist_id: str) -> list[Track]:
    """Fetch all tracks in a playlist.

    Uses /v1/playlists/{id}/items (the new endpoint) directly. The legacy
    /tracks endpoint returns 403 for apps registered after Nov 2024.
    """
    sp = auth_spotify.get_client()
    if not sp:
        return []
    out: list[Track] = []
    offset = 0
    while True:
        try:
            page = _retry(
                sp._get,
                f"playlists/{playlist_id}/items",
                limit=100,
                offset=offset,
                additional_types="track",
            )
        except spotipy.SpotifyException as e:
            if e.http_status == 403:
                raise PlaylistForbiddenError(
                    f"Spotify 403 on playlist {playlist_id}/items: Spotify blocks third-party apps "
                    "from reading playlists the account does not own (followed AND algorithmic)."
                ) from e
            raise
        items = page.get("items") or []
        for item in items:
            if not item:
                continue
            t = item.get("track") or item.get("item")
            if not t or t.get("is_local") or not t.get("id"):
                continue
            if t.get("type") and t.get("type") != "track":
                continue
            artists = ", ".join(a["name"] for a in t.get("artists", []) if a.get("name"))
            isrc = (t.get("external_ids") or {}).get("isrc")
            out.append(Track(
                id=t["id"],
                title=t["name"],
                artist=artists,
                album=(t.get("album") or {}).get("name"),
                isrc=isrc,
                duration_ms=t.get("duration_ms") or 0,
            ))
        if len(items) < 100 or not page.get("next"):
            break
        offset += 100
    return out


def create_playlist(name: str, description: str = "Synced by Plexify") -> Optional[str]:
    sp = auth_spotify.get_client()
    if not sp:
        return None
    me = _retry(sp.current_user)
    pl = _retry(sp.user_playlist_create, me["id"], name, public=False, description=description)
    return pl["id"]


def add_tracks(playlist_id: str, track_ids: Iterable[str]) -> list[str]:
    """Add tracks to a Spotify playlist. Returns list of IDs successfully added (per-chunk)."""
    sp = auth_spotify.get_client()
    if not sp:
        return []
    ids = list(dict.fromkeys(track_ids))
    added: list[str] = []
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        try:
            _retry(sp.playlist_add_items, playlist_id, chunk)
            added.extend(chunk)
        except Exception as e:
            log.exception("Spotify add chunk failed (offset %d): %s", i, e)
            break
    return added


def save_tracks(track_ids: Iterable[str]) -> dict:
    """Add tracks to the user's Spotify Liked Songs (Saved Tracks).

    Returns {"saved": [ids...], "needs_scope": bool}. Requires the
    user-library-modify scope — if the connected token predates that scope, Spotify
    returns 403 and we flag needs_scope so the caller can prompt a reconnect."""
    sp = auth_spotify.get_client()
    if not sp:
        return {"saved": [], "needs_scope": False}
    ids = list(dict.fromkeys([t for t in track_ids if t]))
    saved: list[str] = []
    needs_scope = False
    for i in range(0, len(ids), 50):  # Spotify caps saved-tracks add at 50/call
        chunk = ids[i:i + 50]
        try:
            _retry(sp.current_user_saved_tracks_add, chunk)
            saved.extend(chunk)
        except Exception as e:
            msg = str(e).lower()
            if "403" in msg or "insufficient" in msg or "scope" in msg:
                needs_scope = True
                log.warning("Spotify save_tracks: missing user-library-modify scope — reconnect Spotify")
            else:
                log.exception("Spotify save_tracks chunk failed: %s", e)
            break
    return {"saved": saved, "needs_scope": needs_scope}


def remove_tracks(playlist_id: str, track_ids: Iterable[str]) -> list[str]:
    sp = auth_spotify.get_client()
    if not sp:
        return []
    ids = list(dict.fromkeys(track_ids))
    removed: list[str] = []
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        try:
            _retry(sp.playlist_remove_all_occurrences_of_items, playlist_id, chunk)
            removed.extend(chunk)
        except Exception as e:
            log.exception("Spotify remove chunk failed: %s", e)
            break
    return removed


def search_track(title: str, artist: str, isrc: Optional[str] = None) -> Optional[Track]:
    sp = auth_spotify.get_client()
    if not sp:
        return None
    if isrc:
        try:
            res = _retry(sp.search, q=f"isrc:{isrc}", type="track", limit=1)
            items = (res.get("tracks") or {}).get("items") or []
            if items:
                t = items[0]
                return Track(
                    id=t["id"],
                    title=t["name"],
                    artist=", ".join(a["name"] for a in t.get("artists", [])),
                    album=(t.get("album") or {}).get("name"),
                    isrc=(t.get("external_ids") or {}).get("isrc"),
                    duration_ms=t.get("duration_ms") or 0,
                )
        except Exception as e:
            log.debug("ISRC search failed: %s", e)

    title_clean = _sanitize(title)
    artist_clean = _sanitize(artist)
    queries = [
        f'track:"{title_clean}" artist:"{artist_clean}"',
        f'{title_clean} {artist_clean}',
        f'{_strip_extras(title_clean)} {_strip_extras(artist_clean)}',
    ]
    _search_state.ratelimited = False
    candidates: list[Track] = []
    seen_ids: set[str] = set()
    for q in queries:
        q = q.strip()
        if not q:
            continue
        try:
            res = _retry(sp.search, q=q, type="track", limit=10)
        except Exception as e:
            log.warning("Spotify search failed for %r: %s", q, e)
            if "429" in str(e):
                # Rate-limited: each remaining query form would burn its own full
                # retry budget against the same 429 wall (~30s apiece). Bail out
                # and let callers check was_search_ratelimited() — a 429 is not a
                # "track missing" result.
                _search_state.ratelimited = True
                break
            continue
        items = (res.get("tracks") or {}).get("items") or []
        for t in items:
            tid = t.get("id")
            if not tid or tid in seen_ids:
                continue
            seen_ids.add(tid)
            candidates.append(Track(
                id=tid,
                title=t["name"],
                artist=", ".join(a["name"] for a in t.get("artists", [])),
                album=(t.get("album") or {}).get("name"),
                isrc=(t.get("external_ids") or {}).get("isrc"),
                duration_ms=t.get("duration_ms") or 0,
            ))
        if candidates:
            from .matcher import pick_best
            hit = pick_best(title, artist, None, candidates)
            if hit:
                return hit
    return None
