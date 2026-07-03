"""
Fourth acquisition source: squid.wtf (Qobuz hi-res).

squid.wtf exposes a plain JSON API per service; we use the Qobuz one:
  GET /api/get-music?q=<artist title>&offset=0   → search (no auth)
  GET /api/download-music?track_id=<id>&quality=27 → {"data":{"url": <direct Qobuz CDN FLAC>}}

No browser / captcha needed. We search, pick the best matching downloadable track (prefer
hi-res), fetch the signed CDN url, download + verify the 'fLaC' magic. Evaluated at 12/12 on
songs Soulseek/SpotiFLAC/Telegram couldn't get, so it slots in high in the source order.

Same shape as the other pickers: acquire_track() returns {success, paths, provider, error}.
"""
import os
import re
import json
import time
import urllib.request
import urllib.parse
import logging

log = logging.getLogger("app.squid_adapter")

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
_BREAK_SECONDS = 15 * 60
_MP3_BACKEND_BREAK = 20 * 60   # squid.wtf periodically serves MP3 transcodes for EVERY
_MP3_STREAK = 0                # track (its FLAC backend goes down). When that happens the
_MP3_STREAK_TRIP = 3           # download head is MP3, flac_only rejects it, and we waste
                               # ~15s of the PRIMARY slot on every song. After a short streak
                               # of MP3-only deliveries, cool squid down so the picker fast-
                               # paths to Soulseek/spotiflac (which DO deliver FLAC). It
                               # auto-recovers when the cooldown lapses + a real FLAC lands.


def _base() -> str:
    try:
        from .db import get_config
        return (get_config("squid_base", "https://qobuz.squid.wtf") or "https://qobuz.squid.wtf").rstrip("/")
    except Exception:
        return "https://qobuz.squid.wtf"


def is_enabled() -> bool:
    try:
        from .db import get_config
        return (get_config("autofill_squid_enabled", "1") or "1") == "1"
    except Exception:
        return True


# ── rate-limit cooldown (squid/Qobuz can throttle if hammered) ──────────────
def _in_break() -> float:
    try:
        from .db import get_config
        until = float(get_config("squid_break_until", "0") or "0")
    except Exception:
        return 0.0
    return until if time.time() < until else 0.0


def _set_break(seconds: int) -> None:
    try:
        from .db import set_config
        set_config("squid_break_until", str(time.time() + seconds))
    except Exception:
        pass


def _clear_break() -> None:
    try:
        from .db import set_config
        set_config("squid_break_until", "0")
    except Exception:
        pass


def _norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", "", s)
    s = re.sub(r"\b(feat|ft|featuring)\b.*$", "", s)
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return " ".join(s.split())


def _get_json(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def _match(track, want_title, want_artist) -> tuple:
    """(title_sort, artist_set) — LENGTH-AWARE title match + token-set artist match, each 0-100.

    Uses token_SORT_ratio (not token_SET_ratio) for the title: token_set returns 100 for any shared
    subset, so "It's Always Sunny" scored 100 vs "It's Always Sunny With You", and "sex" scored 100
    vs "Sex Offender Shuffle" — making the picker grab the wrong song. token_sort is length-aware.
    Artist still uses token_set (handles "A, B, C" multi-artist subsets)."""
    nt = _norm(track.get("title"))
    pa = (track.get("performer") or {}).get("name") or \
         ", ".join(a.get("name", "") for a in (track.get("artists") or []))
    na = _norm(pa)
    try:
        from rapidfuzz import fuzz
        ts = fuzz.token_sort_ratio(want_title, nt)
        asr = fuzz.token_set_ratio(want_artist, na) if want_artist else 100
    except Exception:
        ts = 100 if (want_title and want_title == nt) else 0
        asr = 100
    return ts, asr


def _score(track, want_title, want_artist) -> float:
    ts, asr = _match(track, want_title, want_artist)
    sc = ts + 0.5 * asr
    if (track.get("maximum_bit_depth") or 0) >= 24:
        sc += 12
    if not track.get("downloadable", True):
        sc -= 1000
    low = (track.get("title") or "").lower()
    if any(w in low for w in ("karaoke", "tribute", "made famous", "instrumental", "8d", "sped up", "nightcore")):
        if not any(w in (want_title or "").lower() for w in ("karaoke", "instrumental", "sped up")):
            sc -= 80
    return sc


def _gated_pick(tracks, wt, wa):
    """Best downloadable track with a strong length-aware title match (>=88) AND artist match (>=70),
    OR a near-exact title (>=96) which lets a mis-tagged-artist upload through. None if nothing clears
    the gate (so we never grab a same-named wrong song)."""
    best, bs = None, -1
    from .version_match import demo_live_banned as _dlb
    for t in tracks or []:
        if not t.get("downloadable", True):
            continue
        if _dlb(t.get("title") or "", wt):   # (Demo)/(Live) banned for a clean liked song
            continue
        ts, asr = _match(t, wt, wa)
        if not (ts >= 88 and (asr >= 70 or ts >= 96)):
            continue
        sc = _score(t, wt, wa)
        if sc > bs:
            best, bs = t, sc
    return best


def _api_search(qstr, timeout):
    try:
        j = _get_json("%s/api/get-music?q=%s&offset=0" % (_base(), urllib.parse.quote(qstr.strip())), timeout=timeout)
        return ((j.get("data") or {}).get("tracks") or {}).get("items") or []
    except Exception as e:
        log.info("squid search failed (%s): %s", qstr, e)
        return []


def _clean_for_query(s: str) -> str:
    """Strip parenthetical/bracketed qualifiers from the API QUERY string.

    _norm() already ignores them when MATCHING, but the raw query still carried
    them — and Qobuz's search treats "(with Dua Lipa)"-style tokens as strong
    relevance keywords, drowning the real track under lounge/karaoke covers whose
    titles mention the guest ("Made Famous by Dua Lipa"). Manually verified:
      "Calvin Harris One Kiss (with Dua Lipa)" -> 10/10 covers, real track absent
      "Calvin Harris One Kiss"                 -> real track, 4x in the top 4."""
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s or "")
    s = re.sub(r"\s*[-–]\s*(remaster(ed)?|mono|stereo|live|acoustic|"
               r"(19|20)\d{2}|mix|version|edit|take)[^-–]*$", " ", s, flags=re.IGNORECASE)
    return " ".join(s.split())


def search_best(artist: str, title: str, timeout: int = 30):
    """Best matching downloadable Qobuz track dict, or None.

    Query passes (gates unchanged — matching still uses _norm, so this only widens
    WHAT WE ASK Qobuz, never what we accept):
      1. raw "artist title" (works for most catalog)
      2. "artist <cleaned title>" — parentheticals stripped from the QUERY; fixes
         featured-credit titles like "One Kiss (with Dua Lipa)" whose qualifier
         poisons Qobuz search relevance (see _clean_for_query)
      3. cleaned-title only (release tagged under a different/featured artist,
         accepted only on a near-exact title via the gate)."""
    if not (title or "").strip():
        return None
    wt, wa = _norm(title), _norm(artist)
    best = _gated_pick(_api_search((artist + " " + title).strip(), timeout), wt, wa)
    if best is not None:
        return best
    ct = _clean_for_query(title)
    if ct and ct.lower() != (title or "").strip().lower():
        best = _gated_pick(_api_search((artist + " " + ct).strip(), timeout), wt, wa)
        if best is not None:
            return best
    return _gated_pick(_api_search((ct or title).strip(), timeout), wt, wa)


def _download_url(track_id, quality: int = 27, timeout: int = 30):
    base = _base()
    j = _get_json("%s/api/download-music?track_id=%s&quality=%s" % (base, track_id, quality), timeout=timeout)
    return (j.get("data") or {}).get("url")


def _safe(name: str) -> str:
    return re.sub(r'[/\\:*?"<>|]', "-", (name or "").strip()).strip(". ")[:120] or "track"


def acquire_track(artist=None, title=None, dest_dir=None, flac_only=True, timeout_seconds=60) -> dict:
    """Acquire ONE track as hi-res FLAC from squid.wtf. {success, paths, provider, error}."""
    global _MP3_STREAK
    out = {"success": False, "paths": [], "source": "squid", "provider": "squid·qobuz", "error": None}
    if not is_enabled():
        out["error"] = "squid disabled"
        return out
    _bu = _in_break()
    if _bu:
        out["error"] = "squid on cooldown %dm" % max(1, int((_bu - time.time()) / 60))
        return out
    title = (title or "").strip()
    artist = (artist or "").strip()
    if not title:
        out["error"] = "no title"
        return out
    deadline = time.time() + timeout_seconds
    try:
        best = search_best(artist, title, timeout=min(30, timeout_seconds))
        if not best:
            out["error"] = "no match on squid"
            return out
        url = _download_url(best["id"], 27, timeout=min(30, int(max(5, deadline - time.time()))))
        if not url:
            out["error"] = "no download url"
            return out
        os.makedirs(dest_dir, exist_ok=True)
        perf = (best.get("performer") or {}).get("name") or artist
        fname = "%s - %s.flac" % (_safe(perf), _safe(best.get("title") or title))
        dest = os.path.join(dest_dir, fname)
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=max(10, int(deadline - time.time()))) as r, open(dest, "wb") as fh:
            head = r.read(4)
            if flac_only and head[:4] != b"fLaC":
                fh.close()
                try: os.remove(dest)
                except Exception: pass
                _MP3_STREAK += 1
                if _MP3_STREAK >= _MP3_STREAK_TRIP and not _in_break():
                    _set_break(_MP3_BACKEND_BREAK)
                    log.warning("squid: %d consecutive MP3-only deliveries — backend is not serving "
                                "FLAC; cooling down %dm so the picker uses Soulseek/spotiflac",
                                _MP3_STREAK, _MP3_BACKEND_BREAK // 60)
                    _MP3_STREAK = 0
                out["error"] = "not a FLAC (head=%r)" % head
                return out
            fh.write(head)
            while True:
                chunk = r.read(262144)
                if not chunk:
                    break
                fh.write(chunk)
        if os.path.isfile(dest) and os.path.getsize(dest) > 50000:
            _MP3_STREAK = 0
            _clear_break()
            # squid/Qobuz downloads arrive with EMPTY Vorbis tags. Untagged files blind the
            # picker's dedup ledger: _file_song_info() can't identify the song, so owned tracks
            # are never skipped AND newly-imported tracks are never remembered — which makes
            # upgrade-only rows re-acquire the same hi-res file forever (the "Head & Heart" loop).
            # squid already resolved artist/title/isrc to do the search, so stamp them now;
            # the picker fills album/albumartist at placement.
            try:
                from mutagen.flac import FLAC as _FLAC
                _perf = (best.get("performer") or {}).get("name") or artist or ""
                _alb = best.get("album")
                _alb = (_alb.get("title") if isinstance(_alb, dict) else _alb) or ""
                _iso = best.get("isrc") or ""
                _ttl = best.get("title") or title
                _ff = _FLAC(dest)
                if _perf:
                    _ff["artist"] = _perf
                    _ff["albumartist"] = _perf
                if _ttl:
                    _ff["title"] = _ttl
                if _alb:
                    _ff["album"] = _alb
                if _iso:
                    _ff["isrc"] = _iso
                _ff.save()
            except Exception:
                log.info("squid acquire_track: tag-stamp failed for %s (continuing)", dest)
            out["success"] = True
            out["paths"] = [dest]
            out["bit_depth"] = best.get("maximum_bit_depth")
            out["isrc"] = best.get("isrc") or ""
            return out
        try: os.remove(dest)
        except Exception: pass
        out["error"] = "download too small"
        return out
    except Exception as e:
        msg = str(e)
        # 429 / connection refused style → back off so we stop hammering squid/Qobuz
        if "429" in msg or "rate" in msg.lower() or "forbidden" in msg.lower() or "503" in msg:
            _set_break(_BREAK_SECONDS)
        log.info("squid acquire_track failed (%s): %s", title, msg)
        out["error"] = msg[:200]
        return out


def _resolve(track_id):
    """(artist, title, isrc) for a Spotify track id, from the local liked/album mirrors."""
    try:
        from .db import SessionLocal, SpotifyLikedTrack, SpotifyAlbumTrack
        with SessionLocal() as s:
            lt = s.get(SpotifyLikedTrack, track_id)
            if lt:
                return (lt.artist or "", lt.title or "", lt.isrc or "")
            at = s.query(SpotifyAlbumTrack).filter(SpotifyAlbumTrack.track_id == track_id).first()
            if at:
                return ("", at.title or "", at.isrc or "")
    except Exception:
        pass
    return ("", "", "")


def acquire(spotify_url=None, *, track_ids=None, artist=None, album=None, sample_song=None,
            dest_dir=None, flac_only=True, timeout_seconds=120) -> dict:
    """Album/row-level acquire: fetch each of the row's tracks via squid. {success, paths, provider}."""
    out = {"success": False, "paths": [], "source": "squid", "provider": "squid·qobuz", "error": None}
    if not is_enabled() or _in_break():
        out["error"] = "squid unavailable (disabled or cooldown)"
        return out
    deadline = time.time() + timeout_seconds
    paths = []
    todo = []
    for tid in (track_ids or []):
        ra, rt, _ri = _resolve(tid)
        if rt:
            todo.append((ra or artist or "", rt))
    if not todo and sample_song:
        todo = [(artist or "", sample_song)]
    for (ra, rt) in todo:
        if time.time() >= deadline:
            break
        r = acquire_track(artist=ra, title=rt, dest_dir=dest_dir, flac_only=flac_only,
                          timeout_seconds=max(20, int(deadline - time.time())))
        if r.get("success") and r.get("paths"):
            paths.extend(r["paths"])
    out["paths"] = paths
    out["success"] = bool(paths)
    if not paths:
        out["error"] = "squid delivered no tracks"
    return out


# ═══════════════════════════════════════════════════════════════════════════
# NAS-DAEMON DELEGATION (Mac split). On the Mac, the download itself runs on
# the NAS plexify-downloader (:8788); these overrides keep the exact signature
# + return shape of the real implementations above, so picker_tick and every
# other caller is untouched. The implementations above still run verbatim
# inside the daemon on the NAS. (engine/ holds the pristine copy.)
# ═══════════════════════════════════════════════════════════════════════════

_real_acquire = acquire
_real_acquire_track = acquire_track


def acquire(spotify_url=None, *, track_ids=None, artist=None, album=None,
            sample_song=None, dest_dir=None, flac_only=True, timeout_seconds=120):
    if os.environ.get("PLEXIFY_DOWNLOADER_DAEMON") == "1":
        return _real_acquire(spotify_url, track_ids=track_ids, artist=artist,
                             album=album, sample_song=sample_song, dest_dir=dest_dir,
                             flac_only=flac_only, timeout_seconds=timeout_seconds)
    from .nas_downloader import enqueue_and_wait
    r = enqueue_and_wait("squid", mode="album", dest_dir=dest_dir,
                         artist=artist, album=album, sample_song=sample_song,
                         spotify_url=spotify_url, track_ids=track_ids,
                         kwargs={"flac_only": flac_only,
                                 "timeout_seconds": timeout_seconds},
                         timeout_seconds=timeout_seconds)
    r.setdefault("provider", "squid·qobuz")
    return r


def acquire_track(artist=None, title=None, dest_dir=None, flac_only=True,
                  timeout_seconds=60):
    if os.environ.get("PLEXIFY_DOWNLOADER_DAEMON") == "1":
        return _real_acquire_track(artist=artist, title=title, dest_dir=dest_dir,
                                   flac_only=flac_only, timeout_seconds=timeout_seconds)
    from .nas_downloader import enqueue_and_wait
    r = enqueue_and_wait("squid", mode="track", dest_dir=dest_dir,
                         artist=artist, title=title,
                         kwargs={"flac_only": flac_only,
                                 "timeout_seconds": timeout_seconds},
                         timeout_seconds=timeout_seconds)
    r.setdefault("provider", "squid·qobuz")
    return r
