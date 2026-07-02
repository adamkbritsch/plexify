"""Spotify OAuth — a spotipy CacheHandler that persists tokens in the database."""

import json
import logging
import threading
import time
from typing import Optional

import spotipy
from spotipy.oauth2 import SpotifyOAuth
from sqlalchemy import select

from .config import SPOTIFY_REDIRECT_URI, SPOTIFY_SCOPES
from .db import AuthToken, get_config, session_scope

log = logging.getLogger(__name__)

SERVICE = "spotify"

# H11/Q28: cache the Spotipy client instance per access-token+timeout. Saves
# constructing a new spotipy.Spotify (which initializes session state) every
# call. Keyed on access_token so a refresh naturally invalidates.
_CLIENT_LOCK = threading.Lock()        # Q29: protect cache + refresh against concurrent threads
_CACHED_CLIENT: dict = {"key": None, "spotify": None, "expires_at": 0.0}

# Q28: longer timeout for endpoints like /me/tracks paging (50 items + market lookup)
_DEFAULT_TIMEOUT_S = 25


class _DBCacheHandler(spotipy.cache_handler.CacheHandler):
    """Persist Spotify token info in our SQLite row instead of a file."""

    def get_cached_token(self):
        with session_scope() as s:
            row = s.scalar(select(AuthToken).where(AuthToken.service == SERVICE))
            if not row:
                return None
            # C1: corrupt JSON in the row used to crash the whole auth flow.
            # Now we log + return None so spotipy falls through to "no token,
            # need re-auth" rather than 500ing the dashboard.
            try:
                return json.loads(row.payload)
            except (ValueError, TypeError) as e:
                log.warning("spotify auth token row is corrupt (%s); treating as no-token", e)
                return None

    def save_token_to_cache(self, token_info):
        with session_scope() as s:
            row = s.scalar(select(AuthToken).where(AuthToken.service == SERVICE))
            payload = json.dumps(token_info)
            if row:
                row.payload = payload
            else:
                s.add(AuthToken(service=SERVICE, payload=payload))
        # Q13: spotipy's CacheHandler protocol expects the saved value back;
        # without this, refresh_access_token can't chain its own logic cleanly.
        return token_info


def get_oauth() -> Optional[SpotifyOAuth]:
    client_id = get_config("spotify_client_id")
    client_secret = get_config("spotify_client_secret")
    if not (client_id and client_secret):
        return None
    return SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope=SPOTIFY_SCOPES,
        cache_handler=_DBCacheHandler(),
        open_browser=False,
        # show_dialog=True so re-authing always shows Spotify's consent screen — this
        # is what lets an already-connected account grant NEWLY-added scopes (e.g.
        # user-library-modify / playlist-modify) on reconnect instead of silently
        # reusing the old, narrower token.
        show_dialog=True,
    )


def get_authorize_url() -> Optional[str]:
    oauth = get_oauth()
    return oauth.get_authorize_url() if oauth else None


def exchange_code(code: str) -> bool:
    oauth = get_oauth()
    if not oauth:
        return False
    try:
        oauth.get_access_token(code, as_dict=True, check_cache=False)
        # H11/C5: bust the client cache so the next get_client() reads the
        # fresh token + re-detects the (possibly different) Spotify user
        invalidate_client_cache()
        return True
    except Exception as e:
        log.exception("Spotify code exchange failed: %s", e)
        return False


def invalidate_client_cache() -> None:
    with _CLIENT_LOCK:
        _CACHED_CLIENT["key"] = None
        _CACHED_CLIENT["spotify"] = None
        _CACHED_CLIENT["expires_at"] = 0.0
    # Also bust the playlist-list cache since it may be filtered by user id
    try:
        from . import spotify_client
        spotify_client.invalidate_list_cache()
    except Exception:
        pass


def get_client(*, timeout: Optional[int] = None) -> Optional[spotipy.Spotify]:
    """Return an authenticated Spotipy client.

    H11: caches the constructed Spotify instance per access token so dashboard
    renders that call is_authed() multiple times don't re-handshake.
    Q29: serializes refresh under a lock so concurrent threads don't double-refresh.
    Q28: respect caller-supplied timeout (defaults to _DEFAULT_TIMEOUT_S).
    """
    oauth = get_oauth()
    if not oauth:
        return None
    token_info = oauth.cache_handler.get_cached_token()
    if not token_info:
        return None

    # Refresh under lock to avoid two threads racing on refresh_access_token
    if oauth.is_token_expired(token_info):
        with _CLIENT_LOCK:
            # Re-read after acquiring lock — another thread may have refreshed already
            token_info = oauth.cache_handler.get_cached_token()
            if token_info and oauth.is_token_expired(token_info):
                try:
                    token_info = oauth.refresh_access_token(token_info["refresh_token"])
                except Exception as e:
                    log.exception("Spotify refresh failed: %s", e)
                    return None

    if not token_info:
        return None

    access = token_info.get("access_token")
    expires = float(token_info.get("expires_at") or 0)
    timeout_s = timeout if timeout is not None else _DEFAULT_TIMEOUT_S
    key = (access, timeout_s)

    # H11: hot path — return cached instance if token unchanged & not expired
    with _CLIENT_LOCK:
        if (_CACHED_CLIENT["key"] == key
                and _CACHED_CLIENT["spotify"] is not None
                and _CACHED_CLIENT["expires_at"] > time.time() + 10):
            return _CACHED_CLIENT["spotify"]
        sp = spotipy.Spotify(
            auth=access,
            requests_timeout=timeout_s,
            retries=0,
            status_retries=0,
        )
        _CACHED_CLIENT["key"] = key
        _CACHED_CLIENT["spotify"] = sp
        _CACHED_CLIENT["expires_at"] = expires
        return sp


def is_authed() -> bool:
    return get_client() is not None


def revoke() -> None:
    invalidate_client_cache()                  # H11: bust cache so re-auth doesn't see old user
    with session_scope() as s:
        row = s.scalar(select(AuthToken).where(AuthToken.service == SERVICE))
        if row:
            s.delete(row)
