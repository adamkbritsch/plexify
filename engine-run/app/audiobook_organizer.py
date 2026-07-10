"""audiobook_organizer.py — the automated stage between auto-m4b and the Plex library.

auto-m4b merges a dropped multi-file audiobook into a single chapterized .m4b and leaves it,
untagged, in <temp>/untagged/. seanap's documented flow finishes with a MANUAL desktop MP3TAG
step; Plexify replaces that with this module: infer what book the file is, confirm it against
Audible's catalog, pull rich metadata from Audnexus, write the MP4 tags (cover included), and
file it into the library as Audiobooks/<Author>/<Title>/<Title>.m4b — the exact layout the Plex
Music-library + Audnexus agent combination indexes.

Runs NEAR STORAGE (inside the plexify-downloader daemon): MP4 tag writes can rewrite the whole
multi-hundred-MB file, which must never happen across an SMB mount. The Mac engine only proxies
status and triggers Plex scans.

MATCHING IS SKIP-NOT-GUESS: a book below the confidence gate is parked in review/ (with its top
candidates recorded for the UI) rather than tagged with a guess — a wrongly-tagged audiobook is
far more expensive to notice than an unresolved one.

Never raises out of organize_pass / resolve_book; every file move is appended to
DATA_DIR/audiobook_moves.jsonl (same shape as the music recovery ledger) so everything is
reverse-replayable.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import shutil
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

DATA_DIR = os.environ.get("DATA_DIR", "/data")
BOOKS_LEDGER = "audiobook_books.jsonl"
MOVES_LEDGER = "audiobook_moves.jsonl"

_SETTLE_SECS = 120          # a file younger than this may still be written by auto-m4b
_MIN_CONFIDENCE_DEFAULT = 80
_SEARCH_MIN_INTERVAL = 1.0  # be polite to Audible's unauthenticated catalog API
_FAIL_PARK_AFTER = 3        # tagging failures before a file is parked in review/

_last_search_at = [0.0]
_size_memo: dict = {}       # path -> size seen on the previous pass (settle detection)
_fail_counts: dict = {}     # path -> consecutive tagging failures


# ── inference ──────────────────────────────────────────────────────────────────────────────────

_JUNK_RE = re.compile(
    r"\b(unabridged|abridged|audiobook|m4b|mp3|64k|128k|retail|complete)\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"\(?(19|20)\d{2}\)?")
_BRACKET_RE = re.compile(r"\[[^\]]*\]")


def _clean_fragment(s: str) -> str:
    s = _BRACKET_RE.sub(" ", s or "")
    s = _JUNK_RE.sub(" ", s)
    s = _YEAR_RE.sub(" ", s)
    return " ".join(s.replace("_", " ").split()).strip(" -–.")


def infer_book_guess(path: str, tags: Optional[dict] = None) -> dict:
    """{title, author} guess for an untagged m4b. Embedded tags win (auto-m4b copies basic tags
    from the source mp3s when present); else filename patterns 'Author - Title' /
    'Title (Author)' / bare 'Title'."""
    tags = tags or {}
    t_album = (tags.get("album") or "").strip()
    t_artist = (tags.get("albumartist") or tags.get("artist") or "").strip()
    if t_album:
        return {"title": _clean_fragment(t_album) or t_album,
                "author": _clean_fragment(t_artist) or None}

    base = os.path.splitext(os.path.basename(path or ""))[0]
    # 'Title (Author)' — trailing parenthetical that isn't a year
    m = re.match(r"^(?P<title>.+?)\s*\((?P<paren>[^)]+)\)\s*$", base)
    if m and not _YEAR_RE.fullmatch(m.group("paren").strip()):
        return {"title": _clean_fragment(m.group("title")),
                "author": _clean_fragment(m.group("paren")) or None}
    # 'Author - Title' (authors are short; long left sides are almost always the title)
    if " - " in base:
        left, _, right = base.partition(" - ")
        left_c, right_c = _clean_fragment(left), _clean_fragment(right)
        if left_c and right_c and len(left_c.split()) <= 4:
            return {"title": right_c, "author": left_c}
    return {"title": _clean_fragment(base), "author": None}


def read_mp4_tags(path: str) -> dict:
    """Best-effort read of the grouping tags auto-m4b may have carried over."""
    try:
        from mutagen.mp4 import MP4
        f = MP4(path)
        def _first(key):
            v = f.tags.get(key) if f.tags else None
            return str(v[0]) if v else None
        return {"album": _first("\xa9alb"), "artist": _first("\xa9ART"),
                "albumartist": _first("aART"), "title": _first("\xa9nam")}
    except Exception:
        return {}


# ── Audible search + Audnexus metadata ─────────────────────────────────────────────────────────

def audible_search(title: str, author: Optional[str] = None, session=None) -> list:
    """Search Audible's unauthenticated catalog for candidates. Any failure returns [] —
    the caller parks the book for review instead of guessing."""
    import requests as _rq
    session = session or _rq
    # polite pacing across calls (module-global)
    wait = _SEARCH_MIN_INTERVAL - (time.time() - _last_search_at[0])
    if wait > 0:
        time.sleep(wait)
    # response_groups: 'product_desc' carries title/subtitle (product_attrs does NOT — the
    # API returns title:null without it, verified live 2026-07-09), 'contributors' the authors.
    params = {"num_results": "10", "response_groups": "contributors,product_desc",
              "title": title or ""}
    if author:
        params["author"] = author
    out = []
    for attempt in (1, 2):
        try:
            _last_search_at[0] = time.time()
            r = session.get("https://api.audible.com/1.0/catalog/products",
                            params=params, timeout=15,
                            headers={"User-Agent": "Plexify-Audiobooks/1.0"})
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            for p in (r.json().get("products") or []):
                out.append({
                    "asin": p.get("asin"),
                    "title": (p.get("title") or "").strip(),
                    "subtitle": (p.get("subtitle") or "").strip(),
                    "authors": [a.get("name", "") for a in (p.get("authors") or [])],
                })
            return [c for c in out if c["asin"] and c["title"]]
        except Exception as e:
            if attempt == 1 and not (title or "").strip():
                return []
            if attempt == 1:
                log.warning("audible_search retrying (%s)", e)
                time.sleep(2.0)
            else:
                log.warning("audible_search failed: %s", e)
    return []


def pick_candidate(guess: dict, candidates: list,
                   min_confidence: int = _MIN_CONFIDENCE_DEFAULT) -> tuple:
    """(best_candidate | None, score). Title similarity weighted 0.6, author 0.4.
    A known author that matches NO candidate author vetoes even a perfect title —
    same-title different-author books are the classic audiobook mismatch."""
    if not candidates:
        return None, 0
    from rapidfuzz.fuzz import token_set_ratio
    g_title = (guess.get("title") or "").lower()
    g_author = (guess.get("author") or "").lower()
    best, best_score = None, -1
    for c in candidates:
        ts = token_set_ratio(g_title, (c.get("title") or "").lower())
        if g_author:
            asc = max((token_set_ratio(g_author, (a or "").lower())
                       for a in (c.get("authors") or [""])), default=0)
            if asc < 40 and ts >= 90:
                continue                        # author veto: right title, wrong author
            score = int(0.6 * ts + 0.4 * asc)
        else:
            score = int(ts * 0.9)               # no author signal → discount title-only matches
        if score > best_score:
            best, best_score = c, score
    if best is None or best_score < int(min_confidence):
        return None, max(best_score, 0)
    return best, best_score


def fetch_audnexus(asin: str, session=None) -> Optional[dict]:
    """Full book metadata from Audnexus (the same API the Plex agent uses)."""
    import requests as _rq
    session = session or _rq
    try:
        r = session.get(f"https://api.audnex.us/books/{asin}", timeout=20,
                        headers={"User-Agent": "Plexify-Audiobooks/1.0"})
        if r.status_code != 200:
            return None
        d = r.json()
        series = d.get("seriesPrimary") or {}
        return {
            "asin": d.get("asin") or asin,
            "title": (d.get("title") or "").strip(),
            "subtitle": (d.get("subtitle") or "").strip(),
            "authors": [a.get("name", "") for a in (d.get("authors") or [])],
            "narrators": [n.get("name", "") for n in (d.get("narrators") or [])],
            "release_date": d.get("releaseDate") or "",
            "summary": d.get("summary") or "",
            "image": d.get("image") or "",
            "genres": [g.get("name", "") for g in (d.get("genres") or []) if g.get("name")],
            "series": series.get("name") or "",
            "series_position": str(series.get("position") or ""),
            "publisher": d.get("publisherName") or "",
        }
    except Exception as e:
        log.warning("fetch_audnexus(%s) failed: %s", asin, e)
        return None


# ── tagging + filing ───────────────────────────────────────────────────────────────────────────

def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def build_mp4_tags(meta: dict) -> dict:
    """Audnexus metadata → MP4 tag mapping (seanap's convention: narrator in ©wrt, sort tags
    keep series order)."""
    title = meta.get("title") or "Unknown"
    author = (meta.get("authors") or ["Unknown"])[0]
    narrator = (meta.get("narrators") or [""])[0]
    year = (meta.get("release_date") or "")[:4]
    summary = _strip_html(meta.get("summary") or "")
    series = meta.get("series") or ""
    pos = meta.get("series_position") or ""
    sort_album = f"{series} {pos} - {title}".strip() if series else title
    tags = {
        "\xa9alb": title,
        "\xa9nam": title,
        "aART": author,
        "\xa9ART": author,
        "\xa9wrt": narrator,
        "\xa9gen": (meta.get("genres") or ["Audiobook"])[0],
        "desc": summary,
        "\xa9cmt": summary[:255],
        "soal": sort_album,
        "soaa": author,
        "soar": author,
    }
    if year:
        tags["\xa9day"] = year
    if meta.get("asin"):
        tags["----:com.apple.iTunes:ASIN"] = meta["asin"]
    return tags


def _safe(name: str, default: str = "Unknown") -> str:
    """Filesystem-safe path fragment. Mirrors autofill_engine._safe_for_fs (imported when
    available so music + audiobooks sanitize identically)."""
    try:
        from .autofill_engine import _safe_for_fs
        return _safe_for_fs(name, default)
    except Exception:
        s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", (name or "").strip()).strip(". ")
        return s[:200] or default


def dest_for(meta: dict, library_dir: str) -> str:
    author = _safe((meta.get("authors") or ["Unknown"])[0], "Unknown Author")
    title = _safe(meta.get("title") or "Unknown", "Unknown Title")
    return os.path.join(library_dir, author, title, f"{title}.m4b")


def apply_tags(m4b_path: str, meta: dict, cover_bytes: Optional[bytes] = None) -> None:
    """Write the tags (and cover) IN PLACE — the file must be on a local volume (the rewrite can
    touch the whole file)."""
    from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
    f = MP4(m4b_path)
    for key, val in build_mp4_tags(meta).items():
        if key.startswith("----"):
            f[key] = [MP4FreeForm(str(val).encode("utf-8"))]
        else:
            f[key] = [val]
    if cover_bytes:
        fmt = MP4Cover.FORMAT_PNG if cover_bytes[:8] == b"\x89PNG\r\n\x1a\n" else MP4Cover.FORMAT_JPEG
        f["covr"] = [MP4Cover(cover_bytes, imageformat=fmt)]
    f.save()


def _fetch_cover(url: str, session=None) -> Optional[bytes]:
    if not url:
        return None
    import requests as _rq
    session = session or _rq
    try:
        r = session.get(url, timeout=20)
        if r.status_code == 200 and len(r.content) > 1000:
            return r.content
    except Exception:
        pass
    return None


# ── ledgers ────────────────────────────────────────────────────────────────────────────────────

def _ledger_path(name: str) -> str:
    return os.path.join(DATA_DIR, name)


def _log_book(record: dict) -> None:
    record = {"ts": _dt.datetime.utcnow().isoformat() + "Z", **record}
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(_ledger_path(BOOKS_LEDGER), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        log.exception("audiobook ledger write failed")


def _log_ab_move(kind: str, src: str, dst: str) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(_ledger_path(MOVES_LEDGER), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": _dt.datetime.utcnow().isoformat() + "Z",
                                 "kind": kind, "src": src, "dst": dst}) + "\n")
    except Exception:
        log.exception("audiobook move-ledger write failed")


def book_records(limit: int = 30) -> list:
    """Most-recent-first tail of the books ledger (the UI's recent list + review queue)."""
    try:
        with open(_ledger_path(BOOKS_LEDGER), encoding="utf-8") as fh:
            lines = fh.readlines()[-max(1, limit * 3):]
        out = [json.loads(l) for l in lines if l.strip()]
        return list(reversed(out))[:limit]
    except FileNotFoundError:
        return []
    except Exception:
        log.exception("audiobook ledger read failed")
        return []


# ── settle detection ───────────────────────────────────────────────────────────────────────────

def _settled(path: str, size_memo: Optional[dict] = None) -> bool:
    """True when the file is old enough AND its size matches what we saw on the previous pass —
    auto-m4b may still be writing a fresh m4b."""
    memo = _size_memo if size_memo is None else size_memo
    try:
        st = os.stat(path)
    except OSError:
        return False
    prev = memo.get(path)
    memo[path] = st.st_size
    if time.time() - st.st_mtime < _SETTLE_SECS:
        return False
    return prev is not None and prev == st.st_size


# ── the pass ───────────────────────────────────────────────────────────────────────────────────

_PASS_LOCK = threading.Lock()


def _iter_untagged(untagged: str) -> list:
    """(m4b_path, container_dir|None) for every book in untagged/. auto-m4b 'puts the m4b into
    a folder' (observed live: untagged/<Book>/<Book>.m4b + a .chapters.txt sidecar), so books are
    one level DOWN; bare top-level .m4b files are handled too for robustness."""
    out = []
    try:
        entries = sorted(os.listdir(untagged))
    except OSError:
        return out
    for name in entries:
        if name.startswith("."):
            continue
        p = os.path.join(untagged, name)
        if name.lower().endswith(".m4b") and os.path.isfile(p):
            out.append((p, None))
        elif os.path.isdir(p):
            try:
                for fn in sorted(os.listdir(p)):
                    if fn.lower().endswith(".m4b") and not fn.startswith("."):
                        out.append((os.path.join(p, fn), p))
            except OSError:
                continue
    return out


def _cleanup_book_dir(container_dir: Optional[str]) -> None:
    """After the m4b leaves untagged/, drop the leftover sidecars (.chapters.txt — the chapter
    data is embedded in the m4b) and the emptied folder."""
    if not container_dir or not os.path.isdir(container_dir):
        return
    try:
        for fn in os.listdir(container_dir):
            if fn.lower().endswith((".chapters.txt", ".txt", ".jpg", ".png")) or fn.startswith("."):
                try:
                    os.remove(os.path.join(container_dir, fn))
                except OSError:
                    pass
        if not os.listdir(container_dir):
            os.rmdir(container_dir)
    except OSError:
        pass


def _park_for_review(path: str, review_dir: str, guess: dict,
                     candidates: list, score: int, reason: str) -> None:
    os.makedirs(review_dir, exist_ok=True)
    dst = os.path.join(review_dir, os.path.basename(path))
    shutil.move(path, dst)
    _log_ab_move("audiobook_review:" + reason, path, dst)
    _log_book({"status": "review", "file": os.path.basename(path), "reason": reason,
               "guess": guess, "best_score": score,
               "candidates": [{"asin": c.get("asin"), "title": c.get("title"),
                               "authors": c.get("authors")} for c in candidates[:3]]})


def _tag_and_file(path: str, meta: dict, library_dir: str) -> str:
    """Tag in place (local volume), then same-volume rename into the library. Returns dest."""
    size = os.path.getsize(path)
    free = shutil.disk_usage(os.path.dirname(path)).free
    if free < 2 * size:
        raise RuntimeError(f"insufficient free space to retag ({free} < 2x{size})")
    cover = _fetch_cover(meta.get("image") or "")
    apply_tags(path, meta, cover)
    dest = dest_for(meta, library_dir)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.move(path, dest)
    try:
        os.chmod(dest, 0o644)
    except OSError:
        pass
    _log_ab_move("audiobook_organize", path, dest)
    return dest


def organize_pass(temp_dir: str, library_dir: str,
                  min_confidence: int = _MIN_CONFIDENCE_DEFAULT,
                  review_dir: Optional[str] = None) -> dict:
    """One pass over <temp_dir>/untagged/*.m4b: infer → Audible search → confidence gate →
    tag + file (or park in review/). Single-flight; never raises."""
    out = {"seen": 0, "organized": 0, "review": 0, "skipped_unsettled": 0, "errors": 0}
    if not _PASS_LOCK.acquire(blocking=False):
        out["skipped"] = "pass already running"
        return out
    try:
        untagged = os.path.join(temp_dir, "untagged")
        review_dir = review_dir or os.path.join(os.path.dirname(temp_dir.rstrip("/")), "review")
        if not os.path.isdir(untagged):
            out["skipped"] = f"untagged dir missing: {untagged}"
            return out
        for path, container_dir in _iter_untagged(untagged):
            fn = os.path.basename(path)
            out["seen"] += 1
            if not _settled(path):
                out["skipped_unsettled"] += 1
                continue
            try:
                guess = infer_book_guess(path, read_mp4_tags(path))
                candidates = audible_search(guess.get("title") or "", guess.get("author"))
                best, score = pick_candidate(guess, candidates, min_confidence)
                if best is None:
                    _park_for_review(path, review_dir, guess, candidates, score,
                                     "low_confidence" if candidates else "no_candidates")
                    _cleanup_book_dir(container_dir)
                    out["review"] += 1
                    continue
                meta = fetch_audnexus(best["asin"])
                if not meta or not meta.get("title"):
                    _park_for_review(path, review_dir, guess, candidates, score, "audnexus_failed")
                    _cleanup_book_dir(container_dir)
                    out["review"] += 1
                    continue
                dest = _tag_and_file(path, meta, library_dir)
                _cleanup_book_dir(container_dir)
                _log_book({"status": "organized", "file": fn, "title": meta["title"],
                           "author": (meta.get("authors") or ["?"])[0],
                           "asin": meta.get("asin"), "cover_url": meta.get("image"),
                           "score": score, "dest": dest})
                out["organized"] += 1
                _fail_counts.pop(path, None)
            except Exception as e:
                out["errors"] += 1
                log.exception("organize failed for %s", fn)
                n = _fail_counts.get(path, 0) + 1
                _fail_counts[path] = n
                if n >= _FAIL_PARK_AFTER and os.path.exists(path):
                    try:
                        _park_for_review(path, review_dir, {"title": fn}, [], 0,
                                         f"failed_{n}x: {str(e)[:80]}")
                        _cleanup_book_dir(container_dir)
                        out["review"] += 1
                        _fail_counts.pop(path, None)
                    except Exception:
                        log.exception("review-park failed for %s", fn)
    finally:
        _PASS_LOCK.release()
    log.info("audiobook organize_pass: %s", out)
    return out


def resolve_book(file_name: str, review_dir: str, library_dir: str,
                 asin: Optional[str] = None, author: Optional[str] = None,
                 title: Optional[str] = None) -> dict:
    """Resolve a review-parked book: by ASIN (full Audnexus metadata) or manually with
    author+title (books not on Audible — minimal tags; Plex Local Media Assets covers the rest)."""
    safe_name = os.path.basename(file_name or "")
    path = os.path.join(review_dir, safe_name)
    if not safe_name or not os.path.isfile(path):
        return {"ok": False, "error": "file not found in review folder"}
    try:
        if asin:
            meta = fetch_audnexus(asin)
            if not meta or not meta.get("title"):
                return {"ok": False, "error": f"audnexus lookup failed for {asin}"}
        elif author and title:
            meta = {"asin": "", "title": title.strip(), "authors": [author.strip()],
                    "narrators": [], "release_date": "", "summary": "", "image": "",
                    "genres": ["Audiobook"], "series": "", "series_position": ""}
        else:
            return {"ok": False, "error": "need asin, or author + title"}
        dest = _tag_and_file(path, meta, library_dir)
        _log_book({"status": "organized", "file": safe_name, "title": meta["title"],
                   "author": (meta.get("authors") or ["?"])[0], "asin": meta.get("asin"),
                   "cover_url": meta.get("image"), "score": 100, "dest": dest,
                   "resolved": "manual"})
        return {"ok": True, "dest": dest, "title": meta["title"]}
    except Exception as e:
        log.exception("resolve_book failed for %s", safe_name)
        return {"ok": False, "error": str(e)[:200]}


# ── status for the UI ──────────────────────────────────────────────────────────────────────────

def organizer_status(temp_dir: str, library_dir: str,
                     review_dir: Optional[str] = None) -> dict:
    """Stage counts + recent ledger records (each 'organized' record carries the Audnexus CDN
    cover_url so the UI never reads covers over SMB)."""
    review_dir = review_dir or os.path.join(os.path.dirname(temp_dir.rstrip("/")), "review")

    def _count(sub, exts=None):
        d = os.path.join(temp_dir, sub) if sub else review_dir
        try:
            names = [n for n in os.listdir(d) if not n.startswith(".")]
        except OSError:
            return 0
        if exts:
            return sum(1 for n in names if os.path.splitext(n)[1].lower() in exts)
        return len(names)

    records = book_records(30)
    organized_total = 0
    try:
        with open(_ledger_path(BOOKS_LEDGER), encoding="utf-8") as fh:
            organized_total = sum(1 for l in fh if '"status": "organized"' in l or '"status":"organized"' in l)
    except OSError:
        pass
    return {
        "dropped": _count("recentlyadded"),
        "converting": _count("merge") + _count("fix"),
        "untagged": len(_iter_untagged(os.path.join(temp_dir, "untagged"))),
        "review": _count(None, {".m4b"}),
        "organized_total": organized_total,
        "recent": records,
        "library_visible": os.path.isdir(library_dir),
    }
