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


class DestConflictError(RuntimeError):
    """The matched title's library path is already occupied by another file — filing would
    overwrite it. The book goes to review instead (skip-not-guess applies to moves too)."""


# ── inference ──────────────────────────────────────────────────────────────────────────────────

_JUNK_RE = re.compile(
    r"\b(unabridged|abridged|audiobook|m4b|mp3|64k|128k|retail|complete)\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"\(?(19|20)\d{2}\)?")
_BRACKET_RE = re.compile(r"\[[^\]]*\]")
# Audible ASINs are 10 chars starting B0 — rips often embed them: '... [B0FF5CWGK6]'.
# An embedded ASIN is an EXACT product id → skip searching entirely.
_ASIN_RE = re.compile(r"\b(B0[A-Z0-9]{8})\b")
# Multi-part releases (GraphicAudio dramatized adaptations etc.): 'Dark Age (Part 1 of 3)',
# 'Book - Part 2', 'Pt. 3', '(2 of 3)'. Parts must SORT INTO ONE BOOK: same album folder,
# per-part track files ordered by track number — never separate per-part "books".
_PART_RES = (
    re.compile(r"\(\s*part\s*(\d+)\s*of\s*(\d+)\s*\)", re.IGNORECASE),
    re.compile(r"\(\s*(\d+)\s*of\s*(\d+)\s*\)"),
    re.compile(r"[-–—,\s]\s*part\s*(\d+)\b(?:\s*of\s*(\d+))?", re.IGNORECASE),
    re.compile(r"\bpt\.?\s*(\d+)\b(?:\s*of\s*(\d+))?", re.IGNORECASE),
)


def extract_asin(s: str) -> Optional[str]:
    m = _ASIN_RE.search(s or "")
    return m.group(1) if m else None


def strip_part_info(s: str) -> tuple:
    """(name_without_part_marker, part_no|None, part_total|None). Only the part marker is
    removed — edition qualifiers like '(Dramatized Adaptation)' stay, because they distinguish
    genuinely different Audible products of the same book."""
    for rx in _PART_RES:
        m = rx.search(s or "")
        if m:
            part = int(m.group(1))
            total = int(m.group(2)) if m.lastindex and m.lastindex >= 2 and m.group(2) else None
            cleaned = (s[:m.start()] + " " + s[m.end():]).strip()
            cleaned = " ".join(cleaned.split()).strip(" -–—,.")
            return cleaned, part, total
    return s or "", None, None


def _clean_fragment(s: str) -> str:
    s = _BRACKET_RE.sub(" ", s or "")
    s = _JUNK_RE.sub(" ", s)
    s = _YEAR_RE.sub(" ", s)
    s = s.replace("[", " ").replace("]", " ")   # stray unpaired brackets ('…Novel]')
    return " ".join(s.replace("_", " ").split()).strip(" -–.")


# Real-world drop names (observed in Adam's plexify-imports dump) that plain
# 'Author - Title' parsing can't read:
#   '01 FW1.0 Earth Unaware'                       → reading-order + series-code prefix
#   'Book 4 - Harry Potter and the Goblet of Fire (2000)'          → series prefix + year
#   'Dark Age by Pierce Brown Book 5'              → 'Title by Author Book N'
#   'The Iliad {Robert Fitzgerald Transl} read by Dan Stevens'     → braces + narrator suffix
#   'Alexandre Dumas - The Count of Monte Cristo (2008) - John Lee' → trailing narrator segment
#   'Sunrise on the Reaping꞉ A Hunger Games Novel]'                → unicode colon + stray bracket
#   'AManCalledOveUnabridged'                      → CamelCase concatenation
_UNICODE_COLONS = str.maketrans({"꞉": ":", "∶": ":", "：": ":"})
_ORDER_CODE_RE = re.compile(r"^\s*\d{1,3}(?:\.\d+)?\s+(?:[A-Z]{1,4}\d+(?:\.\d+)?\s+)+")
_BOOK_N_PREFIX_RE = re.compile(r"^\s*book\s+\d+\s*[-–—:]\s*", re.IGNORECASE)
_BRACES_RE = re.compile(r"\{[^}]*\}")
_READ_BY_RE = re.compile(r"[\s,(\-–—]*\b(?:read|narrated)\s+by\s+[^-–—]{2,60}$", re.IGNORECASE)
# 'Title by First Last [Book N]' — the author must be 2+ Capitalized words so titles that merely
# contain 'by' ('Death by Chocolate', 'Seduced by the Highlander', 'History by the Numbers')
# don't lose their tail to a fake author. Deliberately CASE-SENSITIVE: IGNORECASE would let
# lowercase words ('the Highlander') satisfy the [A-Z] guard.
_SERIES_BOOK_RE = re.compile(r"^.{2,40}?,?\s+book\s+\d+(?:\.\d+)?$", re.IGNORECASE)
_BY_AUTHOR_RE = re.compile(
    r"^(?P<t>.{3,}?)\s+[bB]y\s+(?P<a>[A-Z][\w.'’-]+(?:\s+[A-Z][\w.'’-]+){1,3})"
    r"(?:[\s,]+[Bb]ook\s+\d+(?:\.\d+)?)?\s*$")


_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z])(?=[A-Z0-9])|(?<=[A-Z])(?=[A-Z][a-z])")


def _normalize_name(s: str, camel: bool = True) -> str:
    """Pre-inference cleanup of a raw drop name: unicode colon stand-ins, CamelCase
    concatenations, reading-order/series-code prefixes, 'Book N -' series prefixes,
    {…} qualifiers and 'read by <narrator>' suffixes.

    CamelCase splitting needs 3+ case boundaries — real run-together names
    ('AManCalledOveUnabridged') have many, while legitimately-CamelCase one-word titles
    ('ReWork', 'SuperFreakonomics') have one and must survive intact. It is also skipped
    for tag values (camel=False): tags are deliberate, not filesystem mangling."""
    s = (s or "").translate(_UNICODE_COLONS)
    if camel and " " not in s and len(_CAMEL_BOUNDARY_RE.findall(s)) >= 3:
        s = _CAMEL_BOUNDARY_RE.sub(" ", s)
    s = _ORDER_CODE_RE.sub("", s)
    s = _BOOK_N_PREFIX_RE.sub("", s)
    s = _BRACES_RE.sub(" ", s)
    s = _READ_BY_RE.sub(" ", s)
    return " ".join(s.split())


def infer_book_guess(path: str, tags: Optional[dict] = None) -> dict:
    """{title, author, asin?, part?, part_total?} guess for an untagged m4b. An ASIN embedded in
    the name ('… [B0FF5CWGK6]') and part markers ('(Part 1 of 3)') are extracted FIRST — from
    the raw name, before cleaning strips the brackets. Embedded tags win for title/author
    (auto-m4b copies basic tags from the source mp3s); else filename patterns 'Author - Title' /
    'Title (Author)' / bare 'Title'."""
    tags = tags or {}
    base = os.path.splitext(os.path.basename(path or ""))[0]
    asin = extract_asin(base) or extract_asin(tags.get("album") or "")
    departed, part, part_total = strip_part_info(base)

    extra = {}
    if asin:
        extra["asin"] = asin
    if part:
        extra["part"], extra["part_total"] = part, part_total

    t_album = (tags.get("album") or "").strip()
    t_artist = (tags.get("albumartist") or tags.get("artist") or "").strip()
    t_title = (tags.get("title") or "").strip()
    if t_album:
        alb_clean, alb_part, alb_total = strip_part_info(t_album)
        if alb_part and "part" not in extra:
            extra["part"], extra["part_total"] = alb_part, alb_total
        # tag values inherit the rip's naming schemes too — same normalization as filenames
        # (minus CamelCase splitting: a tag like 'SuperFreakonomics' is the real title)
        tag_guess = {"title": _clean_fragment(_normalize_name(alb_clean, camel=False)) or t_album,
                     "author": _clean_fragment(_normalize_name(t_artist, camel=False)) or None,
                     **extra}
        # Series-stamped rips: album='Red Rising' (the SERIES) while the track title carries
        # the real book — 'Red Rising Book 04 - Iron Gold'. When the track title CONTAINS the
        # album plus more, derive the actual title from it; the album reading stays as a
        # fallback interpretation.
        if (t_title and len(t_title) > len(t_album) + 3
                and t_album.lower() in t_title.lower()):
            remainder = re.sub(re.escape(t_album), " ", t_title, flags=re.IGNORECASE)
            remainder = re.sub(r"(?i)^[\s\-–—:,]*(?:book\s*\d+(?:\.\d+)?)?[\s\-–—:,]*",
                               "", remainder.strip())
            derived = _clean_fragment(_normalize_name(remainder, camel=False))
            if len(derived) >= 3:
                tag_guess["alts"] = [{"title": tag_guess["title"],
                                      "author": tag_guess.get("author")}]
                tag_guess["title"] = derived
        # Series rips often stamp the SERIES as the album tag: 'Iron Gold by Pierce Brown
        # Book 4.m4b' tagged album='Red Rising' matched (and got FILED AS) 'Red Rising',
        # clobbering the real one — observed live 2026-07-10. When the filename parses into a
        # STRUCTURED author+title reading (someone wrote real metadata into that name) and its
        # title DISAGREES with the tag, the filename leads and the tag reading becomes the
        # fallback interpretation. Bare-blob filenames keep the tag-first behavior.
        fn_guess = _guess_from_name(departed, dict(extra))
        fn_title = fn_guess.get("title") or ""
        if fn_guess.get("author") and len(fn_title) >= 5:
            # SORT ratio, strict threshold: the tag stays primary only when it essentially
            # EQUALS the filename title. Set ratio called 'Catching the Wolf of Wall Street'
            # equal to 'The Wolf of Wall Street' (subset), so a mistagged rip's sequel tag
            # beat the correct filename (live mis-file 2026-07-10).
            from rapidfuzz.fuzz import token_sort_ratio
            if token_sort_ratio(fn_title.lower(), (tag_guess["title"] or "").lower()) < 90:
                fn_guess["alts"] = (fn_guess.get("alts") or []) + [
                    {"title": tag_guess["title"], "author": tag_guess.get("author")}]
                return fn_guess
        return tag_guess

    return _guess_from_name(departed, extra)


def _guess_from_name(departed: str, extra: dict) -> dict:
    """The filename-pattern half of infer_book_guess (tag-free)."""
    base = _normalize_name(departed)
    # 'Title by First Last Book N' — series rips name whole sets this way
    m_by = _BY_AUTHOR_RE.match(base)
    if m_by:
        return {"title": _clean_fragment(m_by.group("t")),
                "author": _clean_fragment(m_by.group("a")), **extra}
    # 'Author - Series, Book N - Title' ('Pierce Brown - Red Rising, Book 5 - Dark Age'):
    # a middle segment that is '<series>, Book N' is series metadata — the real title is what
    # FOLLOWS it. Without this the series name became the title and matched the wrong book
    # (stopped only by the dest-conflict guard; live re-drop 2026-07-10).
    segs = base.split(" - ")
    if len(segs) >= 3 and _SERIES_BOOK_RE.match(segs[1].strip()):
        author_c = _clean_fragment(segs[0])
        title_c = _clean_fragment(" - ".join(segs[2:]))
        if author_c and title_c and len(author_c.split()) <= 4:
            return {"title": title_c, "author": author_c, **extra}

    # 'Author - Title (Year) - Narrator': only drop a trailing segment as the narrator when a
    # (year) in the KEPT segments corroborates the Author-Title-Year reading. Without that
    # anchor, 'Author - Series - Title' rips ('Stephen King - The Dark Tower - The Waste
    # Lands') would lose their real title and mis-tag as the wrong book in the series.
    segs = base.split(" - ")
    if len(segs) >= 3:
        last = segs[-1].strip()
        if (last and len(last.split()) <= 4 and not re.search(r"\d", last)
                and _YEAR_RE.search(" - ".join(segs[:-1]))):
            base = " - ".join(segs[:-1])
    # 'Title: Subtitle' — rips replace the ':' with '_' ('Dark Age (Dramatized Adaptation)_ Red
    # Rising, Book 5'). The subtitle makes the Audible search too specific (every term must
    # match) — search on the main title only.
    m_sub = re.match(r"^(?P<main>.+?)[_:]\s+(?P<sub>[A-Z].{3,})$", base)
    if m_sub and len(m_sub.group("main")) >= 4:
        extra.setdefault("subtitle", _clean_fragment(m_sub.group("sub")))
        base = m_sub.group("main").strip()
    # 'Title (Author)' — trailing parenthetical that isn't a year
    m = re.match(r"^(?P<title>.+?)\s*\((?P<paren>[^)]+)\)\s*$", base)
    if m and not _YEAR_RE.fullmatch(m.group("paren").strip()):
        # a trailing edition qualifier is NOT an author — keep it on the title
        paren = m.group("paren").strip()
        if not re.search(r"adaptation|edition|version|dramatized|graphicaudio|unabridged|abridged",
                         paren, re.IGNORECASE):
            return {"title": _clean_fragment(m.group("title")),
                    "author": _clean_fragment(paren) or None, **extra}
    # 'Author - Title' vs 'Title - Author' (authors are short: ≤4 words). When only the LEFT
    # side is short it's the author; only the RIGHT short → it's the author ('The Wolf of Wall
    # Street - Jordan Belfort'); BOTH short is ambiguous → primary reading plus a swapped
    # alternate the search can fall back to (the author-mismatch veto kills the wrong one).
    if " - " in base:
        left, _, right = base.partition(" - ")
        left_c, right_c = _clean_fragment(left), _clean_fragment(right)
        if left_c and right_c:
            l_short, r_short = len(left_c.split()) <= 4, len(right_c.split()) <= 4
            if l_short and r_short:
                return {"title": right_c, "author": left_c,
                        "alts": [{"title": left_c, "author": right_c}], **extra}
            if l_short:
                return {"title": right_c, "author": left_c, **extra}
            if r_short:
                return {"title": left_c, "author": right_c, **extra}
    return {"title": _clean_fragment(base), "author": None, **extra}


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
    from rapidfuzz.fuzz import token_set_ratio, token_sort_ratio
    g_title = (guess.get("title") or "").lower()
    g_author = (guess.get("author") or "").lower()
    g_part = guess.get("part")
    best, best_score = None, -1
    for c in candidates:
        c_title_clean, c_part, _ = strip_part_info(c.get("title") or "")
        # Multi-part releases list every part as its own product with a near-identical
        # title — the file's part number is the ONLY discriminator. A known-part file must
        # never match a DIFFERENT part's product (its metadata would carry the wrong part).
        if g_part and c_part and c_part != g_part:
            continue
        # Title similarity blends SET ratio (tolerates subtitles/franchise suffixes) with
        # SORT ratio (penalizes extra words) — set ratio alone scores a subset title as a
        # PERFECT match, so 'The Wolf of Wall Street' tied with the sequel 'Catching the
        # Wolf of Wall Street' and 'Ender's Game' picked the Full Cast Audioplay (both
        # live mis-files, 2026-07-10). An exact title now always outranks a superset.
        c_low = (c_title_clean or c.get("title") or "").lower()
        ts = (token_set_ratio(g_title, c_low) + token_sort_ratio(g_title, c_low)) // 2
        if g_author:
            asc = max((token_set_ratio(g_author, (a or "").lower())
                       for a in (c.get("authors") or [""])), default=0)
            if asc < 40 and ts >= 90:
                continue                        # author veto: right title, wrong author
            score = int(0.6 * ts + 0.4 * asc)
        else:
            score = int(ts * 0.9)               # no author signal → discount title-only matches
        if g_part and c_part == g_part:
            score = min(100, score + 5)         # exact part agreement is a strong signal
        if score > best_score:
            best, best_score = c, score
    if best is None or best_score < int(min_confidence):
        return None, max(best_score, 0)
    return best, best_score


def _search_ladder(guess: dict) -> list:
    """Audible search with progressive loosening — every query term must match, so an
    over-specific title (subtitle, edition parentheticals, run-on franchise subtitles like
    '… A Hunger Games Novel') returns 0. Ladder: full title → without parentheticals →
    without a trailing franchise subtitle → title WITHOUT the author constraint (rip author
    fields are often wrong-shaped); stop at the first non-empty rung."""
    title = (guess.get("title") or "").strip()
    author = guess.get("author")
    tried = set()
    rungs = [(title, author),
             (re.sub(r"\([^)]*\)", " ", title).strip(), author),
             (title.split(":")[0].strip(), author),
             (re.sub(r"\bA\s+[A-Z][\w' ]{2,40}\s+Novel\s*$", " ", title).strip(), author)]
    if author:
        rungs.append((title, None))
    for q, a in rungs:
        q = " ".join(q.split())
        if not q or (q.lower(), (a or "").lower()) in tried:
            continue
        tried.add((q.lower(), (a or "").lower()))
        cands = audible_search(q, a)
        if cands:
            return cands
    return []


def search_and_pick(guess: dict, min_confidence: int = _MIN_CONFIDENCE_DEFAULT) -> tuple:
    """(best | None, score, candidates_for_review, guess_used). Runs the search ladder for the
    primary reading of the name and then each alternate interpretation (guess['alts'], e.g. the
    swapped side of an ambiguous 'X - Y'), letting the confidence gate decide which reading was
    right. Candidates from every attempt are pooled so a review parking shows all options."""
    interps = [guess] + [dict(guess, **a) for a in (guess.get("alts") or [])]
    pooled, seen_asins = [], set()
    best_overall_score = 0
    for g in interps:
        cands = _search_ladder(g)
        for c in cands:
            if c.get("asin") not in seen_asins:
                seen_asins.add(c.get("asin"))
                pooled.append(c)
        if not cands:
            continue
        best, score = pick_candidate(g, cands, min_confidence)
        if best is not None:
            return best, score, pooled, g
        best_overall_score = max(best_overall_score, score)
    return None, best_overall_score, pooled, guess


_AUTHOR_ROLE_RE = re.compile(
    r"translat|editor|edited|foreword|introduction|afterword|adapted|adaptation|"
    r"narrat|full cast|graphicaudio", re.IGNORECASE)


def _pick_author(meta: dict) -> str:
    """First authors[] entry that is an actual author, not a contributor role — Audible
    editions list entries like 'translation by John Minford', which must never become the
    library's author folder."""
    authors = [a for a in (meta.get("authors") or []) if a]
    for a in authors:
        if not _AUTHOR_ROLE_RE.search(a):
            return a
    return authors[0] if authors else "Unknown"


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
    author = _pick_author(meta)
    narrator = (meta.get("narrators") or [""])[0]
    year = (meta.get("release_date") or "")[:4]
    summary = _strip_html(meta.get("summary") or "")
    series = meta.get("series") or ""
    pos = meta.get("series_position") or ""
    sort_album = f"{series} {pos} - {title}".strip() if series else title
    part = meta.get("part")
    tags = {
        "\xa9alb": title,                    # all parts share the album → ONE book in Plex
        "\xa9nam": f"{title} - Part {int(part)}" if part else title,
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
    if part:
        tags["trkn"] = [(int(part), int(meta.get("part_total") or 0))]   # track order = part order
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
    """Author/Title/Title.m4b — and for multi-part releases, Author/Title/Title - Part NN.m4b:
    every part of a book lands in the SAME album folder (one Plex album, parts as tracks),
    however far apart the parts were dropped."""
    author = _safe(_pick_author(meta), "Unknown Author")
    title = _safe(meta.get("title") or "Unknown", "Unknown Title")
    part = meta.get("part")
    fname = f"{title} - Part {int(part):02d}.m4b" if part else f"{title}.m4b"
    return os.path.join(library_dir, author, title, fname)


def apply_tags(m4b_path: str, meta: dict, cover_bytes: Optional[bytes] = None) -> None:
    """Write the tags (and cover) IN PLACE — the file must be on a local volume (the rewrite can
    touch the whole file)."""
    from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
    f = MP4(m4b_path)
    for key, val in build_mp4_tags(meta).items():
        if key.startswith("----"):
            f[key] = [MP4FreeForm(str(val).encode("utf-8"))]
        elif isinstance(val, list):          # pre-shaped values (trkn = [(part, total)])
            f[key] = val
        else:
            f[key] = [val]
    if cover_bytes:
        fmt = MP4Cover.FORMAT_PNG if cover_bytes[:8] == b"\x89PNG\r\n\x1a\n" else MP4Cover.FORMAT_JPEG
        f["covr"] = [MP4Cover(cover_bytes, imageformat=fmt)]
    f.save()


def itunes_cover_search(title: str, author: Optional[str] = None, session=None) -> Optional[str]:
    """Cover-art fallback for books Audible can't find — shorts that only exist inside
    collections ('Governor Wiggin' ships in 'Ender's Tribe'), regional catalog gaps. Returns
    the best artwork URL or None. The artist must resemble the author so a title-word
    collision can't attach someone else's cover."""
    import requests as _rq
    from rapidfuzz.fuzz import token_set_ratio
    session = session or _rq
    try:
        r = session.get("https://itunes.apple.com/search",
                        params={"term": f"{title} {author or ''}".strip(),
                                "media": "audiobook", "limit": 5},
                        timeout=15, headers={"User-Agent": "Plexify-Audiobooks/1.0"})
        for item in (r.json().get("results") or []):
            art = item.get("artworkUrl100") or item.get("artworkUrl60") or ""
            artist = (item.get("artistName") or "").lower()
            if not art:
                continue
            if author and token_set_ratio(author.lower(), artist) < 60:
                continue
            return art.replace("100x100", "600x600").replace("60x60", "600x600")
    except Exception:
        log.warning("itunes cover search failed", exc_info=True)
    return None


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


def _ui_safe(rec: dict) -> dict:
    """Feed records cross into the Swift app, whose DTO types guess as string->string —
    internal non-string values (the alts interpretation list) BROKE the whole status decode
    and the page showed 'daemon unreachable' + zeros (live 2026-07-10). Strip them here."""
    rec = dict(rec)
    g = rec.get("guess")
    if isinstance(g, dict):
        rec["guess"] = {k: v for k, v in g.items() if isinstance(v, str)}
    return rec


def book_records(limit: int = 30) -> list:
    """Most-recent-first tail of the books ledger (the UI's recent list + review queue).
    An 'organized' record whose library file has since VANISHED (deleted, moved) is dropped
    from the feed — showing it says the book is in the library when it isn't (Sunrise on the
    Reaping kept appearing after its file was destroyed). The ledger line itself stays."""
    try:
        with open(_ledger_path(BOOKS_LEDGER), encoding="utf-8") as fh:
            lines = fh.readlines()[-max(1, limit * 3):]
        out = []
        for l in lines:
            if not l.strip():
                continue
            rec = json.loads(l)
            dest = rec.get("dest")
            if rec.get("status") == "organized" and dest and not os.path.exists(dest):
                continue
            out.append(_ui_safe(rec))
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
# What the organizer is doing RIGHT NOW — surfaced through organizer_status so the UI can show
# live progress instead of a dead button ('Organize now' gave zero feedback).
_CURRENT_WORK = {"file": None, "stage": None}


def _apply_part_info(meta: dict, guess: dict) -> None:
    """Normalize multi-part releases so all parts sort into ONE book. The Audnexus title for a
    part product carries its own marker ('Dark Age (Part 1 of 3) (Dramatized Adaptation)') —
    strip it into part/part_total on the meta, preferring explicit numbers from the file name.
    The remaining title (edition qualifiers intact) becomes the shared album/book title."""
    m_clean, m_part, m_total = strip_part_info(meta.get("title") or "")
    part = guess.get("part") or m_part
    total = guess.get("part_total") or m_total
    if m_part and m_clean:
        meta["title"] = m_clean
    if part:
        meta["part"] = part
        meta["part_total"] = total


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


def _cleanup_book_dir(container_dir: Optional[str], dest_dir: Optional[str] = None) -> None:
    """After the m4b leaves untagged/, drop the leftover sidecars (.chapters.txt — the chapter
    data is embedded in the m4b) and the emptied folder. Companion .pdf/.epub files (Audible
    'this title includes a PDF') are USER CONTENT: when the book's library folder is known they
    move there, beside the m4b; they are never deleted."""
    if not container_dir or not os.path.isdir(container_dir):
        return
    try:
        for fn in os.listdir(container_dir):
            p = os.path.join(container_dir, fn)
            if os.path.splitext(fn)[1].lower() in (".pdf", ".epub") and dest_dir:
                try:
                    shutil.move(p, os.path.join(dest_dir, fn))
                except OSError:
                    pass
            elif fn.lower().endswith((".chapters.txt", ".txt", ".jpg", ".png")) or fn.startswith("."):
                try:
                    os.remove(p)
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
    """Tag in place (local volume), then publish into the library. Returns dest.

    The untagged tree and the library are SEPARATE bind mounts in the daemon container, so the
    final move is a multi-GB copy, not a rename — and the engine's tick-triggered Plex scan can
    fire while it runs. _move_publish makes the file appear atomically (hidden temp + rename),
    otherwise Plex indexes the half-copied, tagless file as '[Unknown Artist]/[Unknown Album]'
    (observed live with the 1.3GB Count of Monte Cristo)."""
    size = os.path.getsize(path)
    free = shutil.disk_usage(os.path.dirname(path)).free
    if free < 2 * size:
        raise RuntimeError(f"insufficient free space to retag ({free} < 2x{size})")
    dest = dest_for(meta, library_dir)
    if os.path.exists(dest):
        # A file already lives at this title's path — two books matched one product
        # (series-tagged rips, two narrations of one title). Overwriting is silent data loss
        # (destroyed Iron Gold + Dark Age + a Martian narration on 2026-07-10); the caller
        # parks the newcomer for human review instead.
        raise DestConflictError(f"library already has {os.path.basename(dest)}")
    cover = _fetch_cover(meta.get("image") or "")
    apply_tags(path, meta, cover)
    _move_publish(path, dest)
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
                _CURRENT_WORK.update(file=fn, stage="matching")
                guess = infer_book_guess(path, read_mp4_tags(path))
                meta, score, candidates = None, 0, []
                if guess.get("asin"):
                    # An embedded ASIN is an exact product id — no search needed.
                    meta = fetch_audnexus(guess["asin"])
                    score = 100
                if not meta:
                    best, score, candidates, guess = search_and_pick(guess, min_confidence)
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
                _apply_part_info(meta, guess)
                _CURRENT_WORK.update(file=fn, stage="tagging + filing")
                dest = _tag_and_file(path, meta, library_dir)
                _cleanup_book_dir(container_dir, os.path.dirname(dest))
                _log_book({"status": "organized", "file": fn, "title": meta["title"],
                           "author": _pick_author(meta),
                           "asin": meta.get("asin"), "cover_url": meta.get("image"),
                           "score": score, "dest": dest})
                out["organized"] += 1
                _fail_counts.pop(path, None)
            except DestConflictError as e:
                _park_for_review(path, review_dir, guess, candidates, score,
                                 f"dest_conflict: {str(e)[:100]}")
                _cleanup_book_dir(container_dir)
                out["review"] += 1
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
        _CURRENT_WORK.update(file=None, stage=None)
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
            # The human's clean author/title often finds the product the raw drop name
            # couldn't (that's usually why it parked). Borrow the match's COVER, summary and
            # genres — the user's title/author stay authoritative; without this, manual
            # resolves shipped with the rip's embedded art (series compilations, wrong book).
            best, score, _cands, _g = search_and_pick(
                {"title": title.strip(), "author": author.strip()})
            if best:
                rich = fetch_audnexus(best["asin"])
                if rich:
                    meta["image"] = rich.get("image") or ""
                    meta["summary"] = rich.get("summary") or ""
                    meta["genres"] = rich.get("genres") or meta["genres"]
                    meta["narrators"] = rich.get("narrators") or []
                    log.info("manual resolve enriched from %s (score %s)", best["asin"], score)
            if not meta["image"]:
                meta["image"] = itunes_cover_search(title.strip(), author.strip()) or ""
        else:
            return {"ok": False, "error": "need asin, or author + title"}
        _apply_part_info(meta, infer_book_guess(safe_name))
        dest = _tag_and_file(path, meta, library_dir)
        _log_book({"status": "organized", "file": safe_name, "title": meta["title"],
                   "author": _pick_author(meta), "asin": meta.get("asin"),
                   "cover_url": meta.get("image"), "score": 100, "dest": dest,
                   "resolved": "manual"})
        return {"ok": True, "dest": dest, "title": meta["title"]}
    except Exception as e:
        log.exception("resolve_book failed for %s", safe_name)
        return {"ok": False, "error": str(e)[:200]}


# ── status for the UI ──────────────────────────────────────────────────────────────────────────

def review_items(review_dir: str, limit: int = 50) -> list:
    """The review queue as the UI needs it: one entry per m4b ACTUALLY in review/, joined with
    its latest ledger record (guess + candidates). The ledger's recent-window alone is not
    enough — a busy day pushes outstanding review records past the window and the queue
    silently disappears from the page while the files keep waiting (live bug 2026-07-10)."""
    try:
        names = sorted(n for n in os.listdir(review_dir)
                       if n.lower().endswith(".m4b") and not n.startswith("."))
    except OSError:
        return []
    latest: dict = {}
    try:
        with open(_ledger_path(BOOKS_LEDGER), encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("status") == "review" and rec.get("file"):
                    latest[rec["file"]] = rec          # later lines win: latest record per file
    except OSError:
        pass
    out = []
    for n in names[:limit]:
        rec = latest.get(n) or {"status": "review", "file": n, "reason": "unknown",
                                "guess": {}, "candidates": []}
        out.append(_ui_safe(rec))
    return out


def organizer_status(temp_dir: str, library_dir: str,
                     review_dir: Optional[str] = None,
                     import_dir: Optional[str] = None) -> dict:
    """Stage counts + recent ledger records (each 'organized' record carries the Audnexus CDN
    cover_url so the UI never reads covers over SMB). 'dropped' includes books still waiting in
    the unified import folder — the drop stage the user actually sees."""
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
    waiting = imports_waiting(import_dir) if import_dir else 0
    return {
        "dropped": _count("recentlyadded") + waiting,
        "imports_waiting": waiting,
        "converting": _count("merge") + _count("fix"),
        "untagged": len(_iter_untagged(os.path.join(temp_dir, "untagged"))),
        "review": _count(None, {".m4b"}),
        "review_items": review_items(review_dir),
        "converter": converter_status(temp_dir),
        "working_on": (dict(_CURRENT_WORK) if _CURRENT_WORK.get("file") else None),
        "organized_total": organized_total,
        "recent": records,
        "library_visible": os.path.isdir(library_dir),
    }


# ── Plex-side reconcile planning ──────────────────────────────────────────────
# Multi-part books can SPLIT in Plex when parts arrive across separate scans: the Audnexus agent
# matches each part's own product (part-specific album titles), and a track scanned mid-refresh
# can land under a duplicate local:// artist with its own album. The organizer's files and tags
# are already right, so the fix is mechanical — this pure planner decides it from a snapshot and
# the engine's plex_client applies it (keeps the logic unit-testable without a Plex server).

def plan_plex_reconcile(albums: list[dict]) -> dict:
    """albums: [{key, title, dir, agent_matched, tracks: [{key, index, file}]}] — one entry per
    album in the audiobook section, `dir` = the library folder its files live in.

    Returns {merges: [(primary_key, [other_keys])], retitles: [(album_key, title)],
    reindexes: [(track_key, part_number)], refreshes: [album_key]}:
    - albums sharing one book folder merge into the agent-matched (else largest) one
    - a multi-part album is titled after its book folder — any per-part agent title is wrong
    - part tracks whose track number doesn't match their part number get it restored
    - '[Unknown Album]' placeholders (a scan caught a file before its tags existed) get a
      metadata refresh — REFRESH ONLY: never plan a metadata delete (with Plex's media
      deletion enabled that endpoint deletes the FILES; learned the hard way 2026-07-10)
    """
    by_dir: dict[str, list[dict]] = {}
    for a in albums:
        if a.get("dir"):
            by_dir.setdefault(a["dir"], []).append(a)

    merges, retitles, reindexes = [], [], []
    refreshes = [a["key"] for a in albums
                 if (a.get("title") or "").strip().lower() in ("[unknown album]", "")]
    for d, group in by_dir.items():
        primary = sorted(group, key=lambda a: (not a.get("agent_matched"),
                                               -len(a.get("tracks") or []),
                                               str(a.get("key"))))[0]
        if len(group) > 1:
            merges.append((primary["key"], [a["key"] for a in group if a is not primary]))

        multipart = False
        for t in (t for a in group for t in (a.get("tracks") or [])):
            base = os.path.splitext(os.path.basename(t.get("file") or ""))[0]
            _, part, _total = strip_part_info(base)
            if part is None:
                continue
            multipart = True
            if t.get("index") != part:
                reindexes.append((t["key"], part))
        if not multipart:
            continue

        want = os.path.basename(d)
        if (primary.get("title") or "") != want:
            retitles.append((primary["key"], want))
    return {"merges": merges, "retitles": retitles, "reindexes": reindexes,
            "refreshes": refreshes}


# ── import-folder routing (daemon-side) ──────────────────────────────────────────────────────
# The unified drop folder (plexify-imports) lives on the SAME volume as auto-m4b's tree and the
# library, so routing is instant renames — and running it in the daemon (every worker pass)
# removes the Mac/SMB dependency entirely (macOS SMB mounts intermittently go quarantine/
# read-only, which silently killed engine-side routing). The engine's PASS 0 router still covers
# single-host deployments; when both can see the folder the first mover wins and the loser's
# shutil.move raises a caught OSError.
#
# Shapes supported (all observed in one real drop):
#   • bare .m4b/.m4a file            → straight to untagged/<stem>/ (no conversion needed —
#                                      auto-m4b would only shuffle it through its serial queue)
#   • bare .mp3/.aax file            → auto-m4b intake (needs conversion/merge)
#   • folder of mp3s (+cover art)    → auto-m4b intake as ONE book
#   • folder with ONE m4b            → untagged/, book named after the FOLDER (usually the
#                                      informative name)
#   • folder of SEVERAL m4bs         → a COLLECTION: each m4b is its own book, routed
#                                      individually — never merged into one
#   • mixed m4bs + mp3s              → m4bs out individually, the mp3 remainder converts
#   • nested audio ('Book/_/*.mp3')  → the nest is flattened (single nest renamed to the top
#                                      folder's name; multi-dir nests flatten with 'dir - file'
#                                      prefixes so disc order survives)
#   • anything containing FLAC       → left alone (the music pipeline owns FLAC)

_CONVERT_EXTS = {".mp3", ".aax"}
_READY_EXTS = {".m4b", ".m4a"}
# Disposable-only: companion .pdf/.epub are USER CONTENT (Audible "this title includes a PDF")
# and are carried along with the book, never deleted.
_SIDECAR_EXTS = {".jpg", ".jpeg", ".png", ".txt", ".nfo", ".cue", ".log",
                 ".m3u", ".m3u8", ".opf", ".db"}
_COMPANION_EXTS = {".pdf", ".epub"}
# Subfolders that are discs of ONE book (flatten + merge) rather than distinct books:
# CD1 / Disc 2 / Disk_3 / Part 1 / D2 / bare numbers.
_DISC_DIR_RE = re.compile(r"^(?:cd|disc|disk|part|d)?[\s_-]*\d+$", re.IGNORECASE)
_PARTIAL_PREFIX = ".plexify-partial-"
_route_size_memo: dict = {}
_ROUTE_LOCK = threading.Lock()


def _entry_footprint(path: str) -> tuple:
    """(newest_mtime, total_size) over a file or tree — settle detection for import entries."""
    newest, total = 0.0, 0
    try:
        st = os.stat(path)
        newest, total = st.st_mtime, (st.st_size if os.path.isfile(path) else 0)
        if os.path.isdir(path):
            for dp, dns, fns in os.walk(path):
                for fn in fns:
                    try:
                        s = os.stat(os.path.join(dp, fn))
                        newest = max(newest, s.st_mtime)
                        total += s.st_size
                    except OSError:
                        pass
    except OSError:
        return (0.0, -1)
    return (newest, total)


def _import_settled(path: str) -> bool:
    """Old enough AND byte-stable across two worker passes — a user may still be copying in."""
    newest, total = _entry_footprint(path)
    if total < 0:
        return False
    prev = _route_size_memo.get(path)
    _route_size_memo[path] = total
    if time.time() - newest < _SETTLE_SECS:
        return False
    return prev is not None and prev == total


def _classify_import_entry(path: str) -> str:
    """'audiobook' | 'music' | 'skip' for a top-level import-folder entry."""
    name = os.path.basename(path)
    if name.startswith(".") or name == "_unnecessary" or os.path.islink(path):
        return "skip"          # symlinks could pull data from OUTSIDE the drop folder
    if os.path.isfile(path):
        ext = os.path.splitext(path)[1].lower()
        if ext == ".flac":
            return "music"
        return "audiobook" if ext in (_CONVERT_EXTS | _READY_EXTS) else "skip"
    n_flac = n_audio = 0
    for dp, dns, fns in os.walk(path):
        for fn in fns:
            ext = os.path.splitext(fn)[1].lower()
            if ext == ".flac":
                n_flac += 1
            elif ext in (_CONVERT_EXTS | _READY_EXTS):
                n_audio += 1
    if n_flac:
        return "music"
    return "audiobook" if n_audio else "skip"


def _free_slot(parent: str, name: str) -> str:
    """A destination under parent that doesn't collide; '<name>_<epoch>[_<n>]' on collision
    (the counter matters — two collisions in the same second must not share a path)."""
    dst = os.path.join(parent, name)
    if not os.path.exists(dst):
        return dst
    stem, ext = os.path.splitext(name)
    n = 0
    while True:
        suffix = f"_{int(time.time())}" + (f"_{n}" if n else "")
        dst = os.path.join(parent, f"{stem}{suffix}{ext}")
        if not os.path.exists(dst):
            return dst
        n += 1


def _move_publish(src: str, dst: str) -> None:
    """Move src (file or tree) to dst so dst APPEARS atomically. The import folder and the
    auto-m4b/untagged trees are separate bind mounts in the container — same underlying volume,
    but crossing a mount boundary makes shutil.move a slow copy+delete, and a plain copy would
    let auto-m4b / the organizer grab a half-written book (or leave a plausible truncated file
    after a crash). So: copy to a hidden '.plexify-partial-*' sibling first (both consumers skip
    dotfiles), then os.rename onto the final name — same-directory renames are atomic. src is
    only removed after the rename, so a crash mid-copy loses nothing."""
    parent = os.path.dirname(dst)
    os.makedirs(parent, exist_ok=True)
    tmp = os.path.join(parent, _PARTIAL_PREFIX + os.path.basename(dst))
    if os.path.exists(tmp):
        shutil.rmtree(tmp, ignore_errors=True) if os.path.isdir(tmp) else os.remove(tmp)
    try:
        os.rename(src, dst)                     # same-mount fast path (already atomic)
        return
    except OSError:
        pass
    if os.path.isdir(src):
        shutil.copytree(src, tmp)
    else:
        shutil.copy2(src, tmp)
    os.rename(tmp, dst)
    if os.path.isdir(src):
        shutil.rmtree(src, ignore_errors=True)
    else:
        try:
            os.remove(src)
        except OSError:
            pass


def _sweep_stale_partials(*dirs: str) -> None:
    """Remove day-old '.plexify-partial-*' leftovers (a crash mid-copy abandons one; the
    source survived, so the partial is pure junk)."""
    cutoff = time.time() - 86400
    for d in dirs:
        try:
            for name in os.listdir(d):
                if not name.startswith(_PARTIAL_PREFIX):
                    continue
                p = os.path.join(d, name)
                try:
                    if os.path.getmtime(p) < cutoff:
                        shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
                except OSError:
                    pass
        except OSError:
            pass


def _route_ready_file(src: str, untagged: str, book_name: str, out: dict) -> str:
    """One finished m4b/m4a → untagged/<book_name>/ in the organizer's folder-per-book shape.
    When the book name is the richer container-folder name, the file is renamed to match so
    filename inference reads the good name. Returns the book folder."""
    ext = os.path.splitext(src)[1]
    book_dir = _free_slot(untagged, _safe(book_name))
    dst = os.path.join(book_dir, _safe(book_name) + ext)
    _move_publish(src, dst)
    _log_ab_move("audiobook_route:untagged", src, dst)
    out["to_untagged"] += 1
    return book_dir


def _leftovers_only_sidecars(path: str) -> bool:
    for dp, dns, fns in os.walk(path):
        for fn in fns:
            if not fn.startswith(".") and os.path.splitext(fn)[1].lower() not in _SIDECAR_EXTS:
                return False
    return True


def _route_folder(src: str, intake: str, untagged: str, out: dict,
                  book_name: Optional[str] = None, depth: int = 0) -> None:
    name = book_name or os.path.basename(src)
    ready, convert = [], []
    for dp, dns, fns in os.walk(src):
        for fn in sorted(fns):
            if fn.startswith("."):
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext in _READY_EXTS:
                ready.append(os.path.join(dp, fn))
            elif ext in _CONVERT_EXTS:
                convert.append(os.path.join(dp, fn))

    # finished m4bs leave first — one m4b takes the folder's name, several are a collection
    # of individual books named after their files
    if len(ready) == 1 and not convert:
        stem = os.path.splitext(os.path.basename(ready[0]))[0]
        book = name if len(_normalize_name(name)) >= len(_normalize_name(stem)) else stem
        book_dir = _route_ready_file(ready[0], untagged, book, out)
        # Audible companion PDFs/ebooks belong WITH the book — carry them into its untagged
        # folder; the organizer moves them on to the library beside the m4b.
        for dp, dns, fns in os.walk(src):
            for fn in fns:
                if os.path.splitext(fn)[1].lower() in _COMPANION_EXTS:
                    try:
                        _move_publish(os.path.join(dp, fn), os.path.join(book_dir, fn))
                    except OSError:
                        pass
    else:
        for f in ready:
            _route_ready_file(f, untagged, os.path.splitext(os.path.basename(f))[0], out)

    if convert:
        subdirs = {os.path.dirname(os.path.relpath(f, src)) for f in convert}
        if subdirs == {""}:
            # audio at the folder root → the whole folder is auto-m4b's book (covers ride along)
            dst = _free_slot(intake, _safe(name))
            _move_publish(src, dst)
            _log_ab_move("audiobook_route:convert", src, dst)
            out["to_convert"] += 1
            return
        first_level = {d.split(os.sep)[0] for d in subdirs}
        if len(first_level) == 1:
            # everything inside one nest ('Lord Of The Flies/_/*.mp3') → the nest IS the book,
            # renamed to the informative top-level name
            nest = os.path.join(src, first_level.pop())
            dst = _free_slot(intake, _safe(name))
            _move_publish(nest, dst)
            _log_ab_move("audiobook_route:convert", nest, dst)
            out["to_convert"] += 1
        elif all(_DISC_DIR_RE.match(d) for d in first_level):
            # multi-DISC rip (CD1/CD2/…) — ONE book: flatten into a hidden build dir with
            # 'disc - file' names (disc order survives the merge), publish atomically
            dst = _free_slot(intake, _safe(name))
            build = os.path.join(intake, _PARTIAL_PREFIX + os.path.basename(dst))
            shutil.rmtree(build, ignore_errors=True)
            os.makedirs(build)
            for f in convert:
                rel = os.path.dirname(os.path.relpath(f, src)).replace(os.sep, " - ")
                shutil.move(f, os.path.join(build, f"{rel} - {os.path.basename(f)}"))
            os.rename(build, dst)
            _log_ab_move("audiobook_route:convert_flattened", src, dst)
            out["to_convert"] += 1
        elif depth == 0:
            # several NON-disc subfolders = several distinct BOOKS (the classic
            # 'Author/Book1, Book2' layout) — route each one as its own book, named
            # 'Top - Sub' so Author-Title inference gets both halves. Merging them into
            # one m4b would be silent content corruption.
            for sub in sorted(first_level):
                _route_folder(os.path.join(src, sub), intake, untagged, out,
                              book_name=f"{name} - {sub}", depth=1)
        else:
            # nested ambiguity below depth 1 — leave it for a human rather than guess
            log.warning("route_imports: %r has nested non-disc subfolders — left in place", name)
            out["errors"] += 1
            return

    if os.path.isdir(src):
        if _leftovers_only_sidecars(src):
            shutil.rmtree(src, ignore_errors=True)
        else:
            log.warning("route_imports: %r left behind (non-sidecar leftovers)", name)


def route_imports(import_dir: str, temp_dir: str, review_dir: Optional[str] = None) -> dict:
    """One routing pass over the unified import folder. Never raises; per-entry failures are
    counted and logged. Music (FLAC) entries are always left for the music pipeline.
    Single-flight: the 60s worker and an 'Organize now' one-shot must not route the same
    entries concurrently."""
    out = {"to_untagged": 0, "to_convert": 0, "left_for_music": 0,
           "skipped_unsettled": 0, "errors": 0}
    if not _ROUTE_LOCK.acquire(blocking=False):
        out["skipped"] = "route pass already running"
        return out
    try:
        return _route_imports_locked(import_dir, temp_dir, out)
    finally:
        _ROUTE_LOCK.release()


def _route_imports_locked(import_dir: str, temp_dir: str, out: dict) -> dict:
    intake = os.path.join(temp_dir, "recentlyadded")
    untagged = os.path.join(temp_dir, "untagged")
    if not (os.path.isdir(import_dir) and os.path.isdir(intake) and os.path.isdir(untagged)):
        out["skipped"] = "dirs missing"
        return out
    _sweep_stale_partials(intake, untagged)
    try:
        entries = sorted(os.listdir(import_dir))
    except OSError:
        out["skipped"] = "unreadable import dir"
        return out
    # prune settle-memo entries that no longer exist (routed / user-removed) so a later
    # same-named re-drop starts its two-pass stability check fresh
    for k in list(_route_size_memo):
        if not os.path.exists(k):
            _route_size_memo.pop(k, None)
    for entry in entries:
        src = os.path.join(import_dir, entry)
        kind = _classify_import_entry(src)
        if kind == "skip":
            continue
        if kind == "music":
            out["left_for_music"] += 1
            continue
        if not _import_settled(src):
            out["skipped_unsettled"] += 1
            continue
        try:
            if os.path.isfile(src):
                ext = os.path.splitext(entry)[1].lower()
                if ext in _READY_EXTS:
                    _route_ready_file(src, untagged, os.path.splitext(entry)[0], out)
                else:
                    dst = _free_slot(intake, entry)
                    shutil.move(src, dst)
                    _log_ab_move("audiobook_route:convert", src, dst)
                    out["to_convert"] += 1
            else:
                _route_folder(src, intake, untagged, out)
        except OSError:
            out["errors"] += 1
            log.exception("route_imports failed for %r", entry)
    if out["to_untagged"] or out["to_convert"]:
        log.info("audiobook route_imports: %s", out)
        _WAITING_CACHE["ts"] = 0.0        # counts changed — next status poll recomputes
    return out


_WAITING_CACHE = {"dir": "", "ts": 0.0, "n": 0}


def imports_waiting(import_dir: str) -> int:
    """How many audiobook-shaped entries sit in the unified import folder (drop-stage count).
    Classification walks each entry's tree, and this runs on the status request path (the
    engine polls every few seconds) — so the answer is cached for 30s."""
    now = time.time()
    if _WAITING_CACHE["dir"] == import_dir and now - _WAITING_CACHE["ts"] < 30:
        return _WAITING_CACHE["n"]
    try:
        n = sum(1 for e in sorted(os.listdir(import_dir))
                if _classify_import_entry(os.path.join(import_dir, e)) == "audiobook")
    except OSError:
        n = 0
    _WAITING_CACHE.update(dir=import_dir, ts=now, n=n)
    return n


# ── converter (auto-m4b) live progress ─────────────────────────────────────────────────────────
# Observed m4b-tool mechanics (live, 2026-07-10): auto-m4b moves EVERY pending book into merge/
# at once and merges ONE book at a time; m4b-tool converts the book's tracks in parallel into
# untagged/<book>/<book>-tmpfiles/ ('NN-…-finished.m4b' per completed track, '-converting.m4b'
# in flight), then concatenates them into untagged/<book>/<book>.m4b at the end. So:
#   • active book  = the merge/ folder whose untagged -tmpfiles dir (or final m4b) exists
#   • exact progress = finished tracks / source tracks (concat phase reported separately)
#   • the rest of merge/ is the queue, in the alphabetical order auto-m4b works through
_CONVERTER_CACHE = {"ts": 0.0, "val": None, "key": ""}


def converter_status(temp_dir: str, ttl: float = 20.0) -> dict:
    """{active: {book, percent, phase, done, files, src_bytes, stalled} | None,
        queue: [{book, src_bytes, files}]} — cached (the status endpoint polls every few
    seconds; sizing merge/ walks a dozen folders)."""
    now = time.time()
    if (_CONVERTER_CACHE["val"] is not None and _CONVERTER_CACHE["key"] == temp_dir
            and now - _CONVERTER_CACHE["ts"] < ttl):
        return _CONVERTER_CACHE["val"]
    merge = os.path.join(temp_dir, "merge")
    untagged = os.path.join(temp_dir, "untagged")
    active, queue = None, []
    try:
        names = sorted(n for n in os.listdir(merge)
                       if not n.startswith(".") and os.path.isdir(os.path.join(merge, n)))
    except OSError:
        names = []
    for name in names:
        src_bytes = files = 0
        for dp, dns, fns in os.walk(os.path.join(merge, name)):
            for fn in fns:
                if os.path.splitext(fn)[1].lower() in (_CONVERT_EXTS | _READY_EXTS):
                    try:
                        src_bytes += os.path.getsize(os.path.join(dp, fn))
                        files += 1
                    except OSError:
                        pass
        tmp = os.path.join(untagged, name, f"{name}-tmpfiles")
        final = os.path.join(untagged, name, f"{name}.m4b")
        has_final = os.path.exists(final)
        if not os.path.isdir(tmp) and not has_final:
            queue.append({"book": name, "src_bytes": src_bytes, "files": files})
            continue
        done, newest = 0, 0.0
        try:
            for fn in os.listdir(tmp):
                p2 = os.path.join(tmp, fn)
                try:
                    newest = max(newest, os.path.getmtime(p2))
                except OSError:
                    pass
                if fn.endswith("-finished.m4b"):
                    done += 1
        except OSError:
            pass
        if has_final:
            phase, percent = "assembling", 97
            try:
                newest = max(newest, os.path.getmtime(final))
            except OSError:
                pass
        else:
            phase = "converting"
            percent = max(1, int(95 * done / files)) if files else 1
        active = {"book": name, "percent": percent, "phase": phase, "done": done,
                  "files": files, "src_bytes": src_bytes,
                  # no artifact touched for 10+ min = the converter is likely wedged;
                  # the UI can say so instead of lying
                  "stalled": bool(newest) and (now - newest) > 600}
    val = {"active": active, "queue": queue}
    _CONVERTER_CACHE.update(ts=now, val=val, key=temp_dir)
    return val


# ── deletion (soft — always trash, never unlink) ───────────────────────────────────────────────
# Hard-learned rule (2026-07-10, twice): nothing in this pipeline permanently destroys audio.
# Deleting from the UI MOVES the book into <library>/.plexify-trash/<stamp>/… — INSIDE the
# library mount, so the move is one atomic same-filesystem rename (no copy window to race or
# crash through) and the trash shares the library volume's fate instead of living on the
# downloads tree (which has a documented self-wipe incident). Plex ignores dot-directories.
# Emptying the trash is a deliberate manual act outside Plexify.

_DELETE_LOCK_TIMEOUT = 30      # seconds a UI delete waits for an in-flight organize pass


def _trash_root(library_dir: str) -> str:
    return os.path.join(library_dir, ".plexify-trash")


def delete_book(rel_dir: str, library_dir: str, trash_root: Optional[str] = None,
                dest: Optional[str] = None) -> dict:
    """Soft-delete one book folder from the library. Identified by rel_dir ('Author/Title'
    relative to the library root) or an absolute dest path under it. The path is fully
    normalized and containment-checked; exactly one book folder — never an author folder,
    the root, or the trash. Serialized against organize_pass (the organizer may be filing a
    new part INTO this folder right now). Returns the canonical rel_dir so callers clean up
    the right Plex entry."""
    trash_root = trash_root or _trash_root(library_dir)
    root = os.path.realpath(library_dir)
    if dest and not rel_dir:
        cand = os.path.realpath(dest)
        if os.path.isfile(cand):
            cand = os.path.dirname(cand)
        rel_dir = os.path.relpath(cand, root)
    rel = os.path.normpath((rel_dir or "").strip().strip("/"))
    if not rel or rel in (".", "..") or rel.startswith(".." + os.sep) or os.path.isabs(rel):
        return {"ok": False, "error": "bad path"}
    src = os.path.realpath(os.path.join(root, rel))
    if not (src.startswith(root + os.sep) and os.path.isdir(src)):
        return {"ok": False, "error": "not a book folder in the library"}
    # validate depth on the RESOLVED path — immune to '.' components and symlink games
    parts = os.path.relpath(src, root).split(os.sep)
    if len(parts) != 2 or parts[0] in (".", "..") or parts[0].startswith("."):
        return {"ok": False, "error": "refusing — pick exactly one book (Author/Title)"}
    if not _PASS_LOCK.acquire(timeout=_DELETE_LOCK_TIMEOUT):
        return {"ok": False, "error": "organizer is busy filing books — try again shortly"}
    try:
        if not os.path.isdir(src):
            return {"ok": False, "error": "not a book folder in the library"}
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = _free_slot(os.path.join(trash_root, stamp), parts[-1])
        _move_publish(src, dst)
        _log_ab_move("audiobook_delete", src, dst)
        _log_book({"status": "deleted", "file": parts[-1], "title": parts[-1],
                   "author": parts[-2], "trash": dst})
        parent = os.path.dirname(src)
        try:
            if os.path.realpath(parent) != root and not os.listdir(parent):
                os.rmdir(parent)               # emptied author folder
        except OSError:
            pass
        return {"ok": True, "trash": dst, "rel_dir": "/".join(parts)}
    finally:
        _PASS_LOCK.release()


def discard_review(file_name: str, review_dir: str, trash_root: str) -> dict:
    """Discard a review-parked file the user doesn't want — moved to trash/, never unlinked."""
    safe = os.path.basename(file_name or "")
    path = os.path.join(review_dir, safe)
    if not safe or not os.path.isfile(path):
        return {"ok": False, "error": "file not found in review folder"}
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = _free_slot(os.path.join(trash_root, stamp, "review"), safe)
    _move_publish(path, dst)
    _log_ab_move("audiobook_discard", path, dst)
    _log_book({"status": "discarded", "file": safe, "trash": dst})
    return {"ok": True, "trash": dst}
