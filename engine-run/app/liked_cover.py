"""Cover art for the 'Spotify Liked Songs' Plex playlist.

Two covers ship with the app (app/static/covers/): a white STAR on an amber
gradient (default) and the classic white HEART on a blue/purple gradient. Both are
uploaded to the Plex playlist so either can be chosen as the poster; the one named
by the `liked_songs_cover` config is selected as active. Choosing is dedupe-safe:
each uploaded poster's key is remembered, so toggling just re-selects an existing
poster instead of re-uploading.
"""
from __future__ import annotations

import logging
import os

from sqlalchemy import select

from . import plex_client
from .autofill_engine import LIKED_SONGS_SENTINEL
from .db import PlaylistPair, SessionLocal, get_config, set_config

log = logging.getLogger(__name__)

COVERS_DIR = os.path.join(os.path.dirname(__file__), "static", "covers")
COVER_FILES = {"star": "liked_star.png", "heart": "liked_heart.jpg"}
DEFAULT_COVER = "star"


def _liked_plex_playlist(srv):
    with SessionLocal() as s:
        pair = s.scalar(
            select(PlaylistPair).where(
                PlaylistPair.spotify_playlist_id == LIKED_SONGS_SENTINEL
            )
        )
        key = pair.plex_playlist_key if pair else None
    if not key:
        return None
    try:
        return srv.fetchItem(int(key))
    except Exception:
        log.exception("liked_cover: could not fetch Plex playlist key=%s", key)
        return None


def _selected_poster_key(pl):
    try:
        for p in pl.posters():
            if getattr(p, "selected", False):
                return str(getattr(p, "ratingKey", "") or getattr(p, "key", "") or "")
    except Exception:
        pass
    return None


def _upload(pl, which):
    fp = os.path.join(COVERS_DIR, COVER_FILES[which])
    if not os.path.isfile(fp):
        log.warning("liked_cover: cover file missing: %s", fp)
        return None
    pl.uploadPoster(filepath=fp)
    key = _selected_poster_key(pl)  # Plex selects the just-uploaded poster
    if key:
        set_config(f"liked_cover_key_{which}", key)
    return key


def set_liked_songs_cover(which: str = DEFAULT_COVER) -> dict:
    """Make `which` ('star'|'heart') the active Liked Songs poster, keeping the other
    uploaded as a selectable option. Returns a small status dict."""
    which = which if which in COVER_FILES else DEFAULT_COVER
    other = "heart" if which == "star" else "star"

    srv = plex_client._connect()
    if not srv:
        return {"ok": False, "error": "Plex unreachable"}
    pl = _liked_plex_playlist(srv)
    if pl is None:
        return {"ok": False, "error": "Liked Songs playlist not found in Plex yet"}

    # Ensure the OTHER cover is uploaded once (so it stays an option), without
    # disturbing the current selection more than necessary.
    if not get_config(f"liked_cover_key_{other}"):
        _upload(pl, other)

    # Select the chosen cover. Prefer re-selecting an already-uploaded poster
    # (no duplicate); only upload if we don't have it yet or it's gone.
    chosen_key = get_config(f"liked_cover_key_{which}")
    selected = False
    if chosen_key:
        try:
            for p in pl.posters():
                pk = str(getattr(p, "ratingKey", "") or getattr(p, "key", "") or "")
                if pk == chosen_key:
                    pl.setPoster(p)
                    selected = True
                    break
        except Exception:
            log.exception("liked_cover: setPoster by key failed")
    if not selected:
        selected = bool(_upload(pl, which))

    set_config("liked_songs_cover", which)
    log.info("liked_cover: active cover set to %s (ok=%s)", which, selected)
    return {"ok": selected, "which": which, "other_available": True}
