"""
ALBUM-CONSISTENCY WATCHER
=========================
Rolling background scan of /Volumes/MediaVolume3/plexify-music that finds albums filed and/or tagged under
the WRONG album artist — the "telehope filed under Blur" class — and re-files
them under the *canonical* album artist, so Plex groups them correctly.

It decides "correct" with the SAME authority the picker places by:
    _canonical_album_artist():  MusicBrainz cache (authoritative)
                              → Spotify mirror album_artist
                              → soundtrack/cast keyword  → "Various Artists"
                              → fall back to the track's own `artist` tag.

SAFE BY DESIGN
--------------
* DRY-RUN by default (config album_consistency_dryrun=1). In dry-run it only
  appends the *proposed* fixes to the ledger and changes NOTHING, so you can
  review the detections before letting it move anything. Flip to 0 to apply.
* Never deletes. A filename collision in the target folder QUARANTINES the
  source file under /Volumes/MediaVolume3/plexify-music/__album_conflicts/<date>/ instead of overwriting.
* Fully reversible: every applied fix (old→new dir, old albumartist per file)
  is appended to /data/album_consistency_fixes.jsonl; rollback_album_fixes.py
  reverses it.
* Per-folder MAJORITY logic + a confidence gate, so legit multi-artist albums
  (Various Artists soundtracks, featured-guest tracks) are NOT touched.
* Batch-limited (max_fixes per tick) — can never mass-move in one pass.
* Rolling cursor (config album_consistency_cursor) walks a slice of artist
  folders per tick, so each run is cheap and the whole library is re-checked
  continuously.
"""
import os
import json
import shutil
import logging
import threading
from collections import Counter
from datetime import datetime

log = logging.getLogger("app.album_consistency")

MUSIC = "/Volumes/MediaVolume3/plexify-music"
LEDGER = "/data/album_consistency_fixes.jsonl"
CONFLICT_BASE = os.path.join(MUSIC, "__album_conflicts")
# folders the watcher must never recurse into / treat as artists
_SKIP_TOP = ("__album_conflicts", "__autofill_pruned", "_quarantine", "_failed")

_LOCK = threading.Lock()


def _cfg(key, default):
    try:
        from .db import get_config
        return get_config(key, default)
    except Exception:
        return default


def _set_cfg(key, value):
    try:
        from .db import set_config
        set_config(key, str(value))
    except Exception:
        log.exception("album_consistency: set_config(%s) failed", key)


def _nkey(s):
    """Normalized comparison key — reuse the engine's exact normalizer so our
    'same artist?' test matches how the picker dedups (Beyoncé == Beyonce etc.)."""
    try:
        from .autofill_engine import _normalize_for_key
        return _normalize_for_key(s or "")
    except Exception:
        return (s or "").strip().lower()


import re as _re

# Stopwords ignored in the shared-token test so "Baljeet and Buford" vs
# "Phineas and Ferb" don't look "consistent" just because both contain "and".
_STOP = {"the", "and", "feat", "ft", "featuring", "with", "of", "in", "on", "to",
         "for", "a", "an", "vs", "vol", "pt", "part", "various", "artists"}


def _strip_feat(s):
    """Drop a trailing featured-credit: 'Alesso feat. Liam Payne' -> 'Alesso'.
    Only strips the unambiguous feat/ft/featuring markers — NOT '&' or ',', which
    can be real band names ('Simon & Garfunkel', 'Earth, Wind & Fire')."""
    s = s or ""
    s = _re.split(r"\s*\(?\s*\b(?:feat|ft|featuring)\b\.?", s, maxsplit=1, flags=_re.IGNORECASE)[0]
    return s.strip().rstrip("([-").strip()


def _consistent(folder_artist, track_artist):
    """Loose 'does this track plausibly belong under this folder artist?' test:
    equal / substring / a shared significant (>=3 char, non-stopword) token after
    normalize. Used both for the no-MB-data fallback AND the anti-fragmentation
    gate, so a clean base-artist folder is never split into a feat/collab variant."""
    f = _nkey(folder_artist)
    a = _nkey(track_artist)
    if not a or not f:
        return True            # untagged → never flag
    if f == a or f in a or a in f:
        return True
    shared = {w for w in (set(f.split()) & set(a.split())) if len(w) >= 3 and w not in _STOP}
    return bool(shared)


def _append_ledger(entry):
    _append_ledger_path(LEDGER, entry)


def _append_ledger_path(path, entry):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        log.exception("album_consistency: ledger append failed (%s)", path)


def _read_tags(path):
    try:
        from .autofill_engine import _read_file_tags
        return _read_file_tags(path)
    except Exception:
        return {}


def _canon(artist, album):
    from .autofill_engine import _canonical_album_artist
    return _canonical_album_artist(artist, album)


def _safe(name, default):
    from .autofill_engine import _safe_for_fs
    return _safe_for_fs(name, default)


def _stamp(path, artist, album, album_artist):
    try:
        from .autofill_engine import _stamp_file_tags
        return _stamp_file_tags(path, artist, album, album_artist=album_artist)
    except Exception:
        log.exception("album_consistency: stamp failed %s", path)
        return False


def _plex_rescan():
    """Best-effort: ask Plex to re-scan the music section so it re-reads the moved
    files. Swallows everything — the Plex URL may be unreachable on LAN."""
    try:
        from . import plex_client
        plex = plex_client._connect()
        if not plex:
            return
        sec = plex_client._music_section(plex)
        if sec:
            sec.update()
            log.info("album_consistency: triggered Plex section refresh")
    except Exception:
        log.info("album_consistency: Plex refresh skipped (unreachable)")


def _decide_target(art_name, tags):
    """Return (target_album_artist, dom_artist, album_tag, confident) or None to skip.

    target = what the album SHOULD be filed/tagged under. None ⇒ leave it alone."""
    artists = [t["artist"] for t in tags if t.get("artist")]
    if not artists:
        return None
    albums = [t["album"] for t in tags if t.get("album")]
    dom_artist, dom_n = Counter(artists).most_common(1)[0]
    album_tag = (Counter(albums).most_common(1)[0][0] if albums else "")

    try:
        canon, confident = _canon(dom_artist, album_tag)
    except Exception:
        return None

    if confident:
        target = canon
    else:
        # No authoritative answer. Only act when the on-disk folder matches NONE
        # of the tracks' artists (a clear mis-file like Blur≠telehope), AND there
        # is a single dominant artist (≥60% of tagged tracks). Otherwise a real
        # compilation / featured-guest album is left untouched.
        if any(_consistent(art_name, a) for a in artists):
            return None
        if dom_n < max(1, int(0.6 * len(artists))):
            return None
        target = dom_artist

    target = (target or "").strip()
    if not target or _nkey(target) in ("", "unknown artist", "unknownartist", "various", "va"):
        # never move things INTO an Unknown bucket; "Various Artists" is allowed
        if _nkey(target) not in ("various artists", "variousartists"):
            return None
    return target, dom_artist, album_tag, confident


def album_consistency_tick(max_folders: int = 40, max_albums: int = 250, max_fixes: int = 20) -> dict:
    """One rolling slice of the /Volumes/MediaVolume3/plexify-music consistency sweep. Returns a summary dict."""
    out = {"enabled": False, "dryrun": True, "scanned_folders": 0, "scanned_albums": 0,
           "flagged": 0, "fixed": 0, "cursor": 0, "skipped": False}
    if (_cfg("album_consistency_enabled", "1") or "1") != "1":
        return out
    if not _LOCK.acquire(blocking=False):
        out["skipped"] = True
        return out
    try:
        out["enabled"] = True
        dryrun = (_cfg("album_consistency_dryrun", "1") or "1") == "1"
        out["dryrun"] = dryrun
        if not os.path.isdir(MUSIC):
            return out

        artist_dirs = sorted(
            d for d in os.listdir(MUSIC)
            if os.path.isdir(os.path.join(MUSIC, d))
            and not d.startswith((".", "_"))
            and d not in _SKIP_TOP
        )
        if not artist_dirs:
            return out

        try:
            cursor = int(_cfg("album_consistency_cursor", "0") or 0)
        except Exception:
            cursor = 0
        if cursor >= len(artist_dirs):
            cursor = 0
        sl = artist_dirs[cursor:cursor + max_folders]
        next_cursor = cursor + max_folders
        if next_cursor >= len(artist_dirs):
            next_cursor = 0
        out["cursor"] = next_cursor

        any_fixed = False
        fixed = 0
        for art_name in sl:
            if fixed >= max_fixes or out["scanned_albums"] >= max_albums:
                break
            art_path = os.path.join(MUSIC, art_name)
            out["scanned_folders"] += 1
            try:
                albums = sorted(d for d in os.listdir(art_path)
                                if os.path.isdir(os.path.join(art_path, d)))
            except Exception:
                continue
            for alb_name in albums:
                if fixed >= max_fixes or out["scanned_albums"] >= max_albums:
                    break
                alb_path = os.path.join(art_path, alb_name)
                try:
                    files = [f for f in os.listdir(alb_path) if f.lower().endswith(".flac")]
                except Exception:
                    continue
                if not files:
                    continue
                out["scanned_albums"] += 1
                tags = []
                for f in files:
                    t = _read_tags(os.path.join(alb_path, f))
                    t["_f"] = f
                    tags.append(t)

                decision = _decide_target(art_name, tags)
                if not decision:
                    continue
                target, dom_artist, album_tag, confident = decision
                target = _strip_feat(target)
                if not target:
                    continue

                tn = _nkey(target)
                folder_ok = (tn == _nkey(art_name))
                # ANTI-OSCILLATION GATE: never move files OUT of a Various Artists
                # folder. VA is a valid consolidation sink; a comp split across both a
                # "Various Artists" folder and a performer folder would otherwise
                # ping-pong (the VA fragment -> performer, the performer -> VA). Once in
                # VA, leave it. (A "V.A"/"VA" variant folder -> "Various Artists" is still
                # allowed, since that target IS Various Artists.)
                if _nkey(art_name) in ("various artists", "various", "va", "v a") \
                        and tn not in ("various artists", "variousartists"):
                    continue
                # ANTI-FRAGMENTATION GATE: never split a clean base-artist folder into
                # a feat/collab variant. If the on-disk folder is a base/subset of (or
                # shares a real token with) the target, the FOLDER is the better album
                # artist — leave the files where they are. Only a folder that is wholly
                # UNRELATED to the target is a genuine mis-file worth moving.
                if not folder_ok and _consistent(art_name, target):
                    continue
                aas = [t.get("albumartist", "") for t in tags]
                tag_ok = bool([a for a in aas if a]) and all(
                    _nkey(a) == tn for a in aas if a
                )
                if folder_ok and tag_ok:
                    continue   # already consistent — nothing to do

                out["flagged"] += 1
                safe_art = _safe(target, "Unknown Artist")
                safe_alb = _safe(alb_name, "Unknown Album")
                target_dir = os.path.join(MUSIC, safe_art, safe_alb)
                move_needed = os.path.normpath(target_dir) != os.path.normpath(alb_path)

                entry = {
                    "ts": datetime.utcnow().isoformat() + "Z",
                    "src_dir": alb_path, "dst_dir": target_dir,
                    "old_folder_artist": art_name, "new_album_artist": target,
                    "album": album_tag, "move": move_needed, "confident": confident,
                    "files": [t["_f"] for t in tags],
                    "old_tags": [{"f": t["_f"], "aa": t.get("albumartist", ""),
                                  "alb": t.get("album", "")} for t in tags],
                }

                if dryrun:
                    _append_ledger({**entry, "applied": False, "dryrun": True})
                    log.info("album_consistency[DRY]: would re-file '%s/%s' -> albumartist=%r dir=%s",
                             art_name, alb_name, target, target_dir)
                    fixed += 1
                    continue

                # ---- APPLY ----
                try:
                    os.makedirs(target_dir, exist_ok=True)
                    os.chmod(target_dir, 0o775)
                    os.chmod(os.path.dirname(target_dir), 0o775)
                except Exception:
                    pass
                moved = []
                for t in tags:
                    src_f = os.path.join(alb_path, t["_f"])
                    if not os.path.exists(src_f):
                        continue
                    # Re-stamp the canonical album artist (preserves per-track performer).
                    _stamp(src_f, t.get("artist") or dom_artist, album_tag or alb_name, target)
                    if not move_needed:
                        continue
                    dst_f = os.path.join(target_dir, t["_f"])
                    if os.path.exists(dst_f):
                        qdir = os.path.join(CONFLICT_BASE, datetime.utcnow().strftime("%Y-%m-%d"),
                                            safe_art, safe_alb)
                        try:
                            os.makedirs(qdir, exist_ok=True)
                            shutil.move(src_f, os.path.join(qdir, t["_f"]))
                            log.warning("album_consistency: collision, quarantined %s", src_f)
                        except Exception:
                            log.exception("album_consistency: quarantine failed %s", src_f)
                        continue
                    try:
                        shutil.move(src_f, dst_f)
                        try:
                            os.chmod(dst_f, 0o664)
                        except Exception:
                            pass
                        moved.append(dst_f)
                    except Exception:
                        log.exception("album_consistency: move failed %s -> %s", src_f, dst_f)

                # carry the cover over, then drop now-empty source dirs (never recursive rm)
                if move_needed:
                    src_cover = os.path.join(alb_path, "cover.jpg")
                    dst_cover = os.path.join(target_dir, "cover.jpg")
                    if os.path.isfile(src_cover) and not os.path.exists(dst_cover):
                        try:
                            shutil.move(src_cover, dst_cover)
                        except Exception:
                            pass
                    try:
                        if not os.listdir(alb_path):
                            os.rmdir(alb_path)
                        if not os.listdir(art_path):
                            os.rmdir(art_path)
                    except Exception:
                        pass

                _append_ledger({**entry, "applied": True, "moved": moved})
                log.info("album_consistency: re-filed '%s/%s' -> albumartist=%r dir=%s (moved %d)",
                         art_name, alb_name, target, target_dir, len(moved))
                any_fixed = True
                fixed += 1

        out["fixed"] = fixed
        _set_cfg("album_consistency_cursor", next_cursor)
        _set_cfg("album_consistency_last_run", datetime.utcnow().isoformat() + "Z")
        if any_fixed:
            _plex_rescan()
        log.info("album_consistency_tick: scanned %d folders / %d albums, flagged %d, %s %d (cursor->%d)",
                 out["scanned_folders"], out["scanned_albums"], out["flagged"],
                 "would-fix" if dryrun else "fixed", fixed, next_cursor)
        return out
    finally:
        _LOCK.release()


# ============================================================================
# PLEX ALBUM AUDIT  (part of the telehope process)
# ----------------------------------------------------------------------------
# The file-level watcher above guarantees on-disk tags/placement are right, but
# Plex can STILL show one album as several "tiles" (separate album objects) even
# when the files are identical and in one folder — incremental adds, or
# case/punctuation tag variants ("It's Time" vs "It's Time" with a curly quote,
# "Horizons/West" vs "Horizons-West"). This audits Plex's *actual* album grouping
# and merges the tiles that are the same album (same album-artist + album,
# ignoring case/punctuation) back into one, then reports edition / same-core
# clusters for human review (those may be intentional — a Deluxe edition, etc.).
#
# SAFE: dry-run by default (album_audit_apply=0) — it only writes a report and
# changes nothing. Reversible: a Plex merge is undone with "Split Apart" in the
# Plex UI, and every merge is logged to /data/plex_album_audit.jsonl. Batch-
# limited per run. Read-only toward the filesystem (never moves/retags files).
# ============================================================================
_AUDIT_LOCK = threading.Lock()
PLEX_AUDIT_LEDGER = "/data/plex_album_audit.jsonl"
PLEX_AUDIT_REPORT = "/data/plex_album_audit_report.txt"

_ED_WORDS = {"deluxe", "remaster", "remastered", "edition", "expanded", "version", "bonus",
             "special", "anniversary", "mono", "stereo", "reissue", "explicit", "ep", "single",
             "original", "motion", "picture", "soundtrack", "disc", "cd", "vol", "volume", "pt",
             "part", "live", "the", "a", "an"}


def _album_core(s):
    """Album title with edition qualifiers + numbers stripped, for clustering
    'X' / 'X (Deluxe)' / 'X (2013 Remaster)' so they can be flagged for review."""
    return " ".join(w for w in _nkey(s).split() if w not in _ED_WORDS and not w.isdigit())


def plex_album_audit_tick(max_merges: int = 40) -> dict:
    """One pass of the Plex album-grouping audit. Returns a summary dict.

    Merges Plex tiles that are the same (album-artist, album) into one (apply mode),
    and always writes a fresh human-readable report to PLEX_AUDIT_REPORT."""
    out = {"enabled": False, "tiles": 0, "split_clusters": 0, "redundant_tiles": 0,
           "merged": 0, "edition_clusters": 0, "dryrun": True, "error": None, "skipped": False}
    if (_cfg("album_audit_enabled", "1") or "1") != "1":
        return out
    if not _AUDIT_LOCK.acquire(blocking=False):
        out["skipped"] = True
        return out
    try:
        out["enabled"] = True
        apply = (_cfg("album_audit_apply", "0") or "0") == "1"
        out["dryrun"] = not apply
        try:
            from . import plex_client
            plex = plex_client._connect()
            sec = plex_client._music_section(plex) if plex else None
        except Exception as e:
            out["error"] = "plex connect: %s" % str(e)[:120]
            return out
        if not sec:
            out["error"] = "plex music section unreachable"
            return out
        try:
            albums = sec.albums()
        except Exception as e:
            out["error"] = "albums(): %s" % str(e)[:120]
            return out
        out["tiles"] = len(albums)

        # group tiles by normalized (album-artist, album); 2+ => Plex split one album
        groups = {}
        for a in albums:
            groups.setdefault((_nkey(a.parentTitle), _nkey(a.title)), []).append(a)
        # cluster distinct titles sharing an album-core (editions) for the review report
        ecore = {}
        for a in albums:
            ecore.setdefault((_nkey(a.parentTitle), _album_core(a.title) or _nkey(a.title)), set()).add(a.title)

        merge_plan = []
        for key, tiles in groups.items():
            if len(tiles) < 2:
                continue
            out["split_clusters"] += 1
            out["redundant_tiles"] += len(tiles) - 1
            # keep the richest tile (most tracks, then lowest ratingKey) as primary
            tiles.sort(key=lambda a: (-(a.leafCount or 0), int(a.ratingKey)))
            primary, others = tiles[0], tiles[1:]
            merge_plan.append({
                "artist": primary.parentTitle, "album": primary.title,
                "primary_rk": int(primary.ratingKey),
                "merge_rks": [int(t.ratingKey) for t in others],
                "track_counts": [t.leafCount for t in tiles],
            })

        editions = {k: sorted(v) for k, v in ecore.items() if len(v) > 1}
        out["edition_clusters"] = len(editions)

        try:
            with open(PLEX_AUDIT_REPORT, "w") as fh:
                fh.write("Plex album audit @ %sZ — %d tiles  (mode: %s)\n"
                         % (datetime.utcnow().isoformat(), len(albums), "APPLY" if apply else "dry-run"))
                fh.write("\n## SPLIT ALBUMS — same album, multiple Plex tiles (auto-merge target)\n")
                fh.write("   %d clusters, %d redundant tiles\n" % (out["split_clusters"], out["redundant_tiles"]))
                for m in sorted(merge_plan, key=lambda m: -len(m["merge_rks"])):
                    fh.write("  x%d  %-26s | %-36s tracks=%s\n"
                             % (len(m["track_counts"]), (m["artist"] or "")[:26], (m["album"] or "")[:36], m["track_counts"]))
                fh.write("\n## EDITION / SAME-CORE CLUSTERS — review, may be intentional (NOT auto-merged)\n")
                fh.write("   %d clusters\n" % len(editions))
                for (ar, co), titles in sorted(editions.items()):
                    fh.write("  %-22s :: %s\n" % (ar[:22], "  |  ".join(t[:34] for t in titles)))
        except Exception:
            log.exception("plex_album_audit: report write failed")

        if not apply:
            log.info("plex_album_audit[DRY]: %d tiles, %d split clusters (%d redundant), %d edition clusters",
                     len(albums), out["split_clusters"], out["redundant_tiles"], out["edition_clusters"])
            return out

        merged = 0
        for m in merge_plan:
            if merged >= max_merges:
                break
            ids = ",".join(str(r) for r in m["merge_rks"])
            try:
                plex.query("/library/metadata/%s/merge?ids=%s" % (m["primary_rk"], ids),
                           method=plex._session.put)
                _append_ledger_path(PLEX_AUDIT_LEDGER,
                                    {**m, "ts": datetime.utcnow().isoformat() + "Z", "action": "merged"})
                merged += 1
            except Exception as e:
                _append_ledger_path(PLEX_AUDIT_LEDGER,
                                    {**m, "ts": datetime.utcnow().isoformat() + "Z",
                                     "action": "merge-failed", "err": str(e)[:160]})
                log.warning("plex_album_audit: merge failed %s — %s: %s",
                            m["artist"], m["album"], str(e)[:120])
        out["merged"] = merged
        if merged:
            _plex_rescan()
        log.info("plex_album_audit: merged %d of %d split clusters (%d tiles)",
                 merged, out["split_clusters"], len(albums))
        return out
    finally:
        _AUDIT_LOCK.release()
