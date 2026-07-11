"""audiobook_suggestor.py — find books like the library's, and fetch the wanted ones.

Two halves, both running in the plexify-downloader daemon (near storage, 24/7 — the
multi-day retry loop must not depend on the Mac being awake):

SUGGEST: build a "you might want these" list from the organized-books ledger —
  • series/author gaps: Audible author search for the library's most-collected authors
  • taste matches: Audible's similar-products endpoint seeded by recently added books
Both endpoints are the same unauthenticated catalog API the organizer already uses
(response_groups MUST include product_desc — product_attrs alone returns title:null).
Suggestions are cached to DATA_DIR and refreshed at most daily (or on demand).

WANT/ACQUIRE: clicking Download in the UI adds a want. Each daemon worker pass drives a
small state machine per want:
  wanted --search hit--> downloading --all transfers done--> delivered (files moved into
  the unified import folder, where the router + auto-m4b + organizer take over — the
  matcher's skip-not-guess gate is the quality control)
  wanted --no result----> wanted again later (backoff: 2h, 6h, 12h, then daily) and
  gave_up after ~5 days, exactly the "try again over the next couple days" contract.

Everything the UI shows is verified LIVE on Soulseek (a ~30s slskd probe per book — the
good peer often responds ~28-30s in, so a shorter window misses it; slskd 429s on parallel
searches so probes are sequential + paced). Result picking is audiobook-shaped and guarded
against title-word collisions: a music track whose path merely contains the title words is
rejected unless an author-name hit OR a runtime-corroborated size confirms it's the book.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from typing import Optional

log = logging.getLogger(__name__)

DATA_DIR = os.environ.get("DATA_DIR", "/data")
WANTS_FILE = "audiobook_wants.json"
DISMISSED_FILE = "audiobook_dismissed.json"

# attempts -> hours until the next try; past the end = give up (~5 days total)
_BACKOFF_HOURS = [2, 6, 12, 24, 24, 24, 24]
_DOWNLOAD_TIMEOUT_S = 45 * 60
_AUDIO_EXTS = {".m4b", ".m4a", ".mp3"}
_BOOK_JUNK_RE = re.compile(r"\b(sample|preview|excerpt|trailer)\b", re.IGNORECASE)

_last_api_call = [0.0]


def _path(name: str) -> str:
    return os.path.join(DATA_DIR, name)


def _load_json(name: str, default):
    try:
        with open(_path(name), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _save_json(name: str, value) -> None:
    tmp = _path(name) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(value, fh, indent=1)
    os.replace(tmp, _path(name))


# ── Audible catalog access ─────────────────────────────────────────────────────────────────────

def _audible_get(path: str, params: dict) -> dict:
    """Paced unauthenticated catalog GET; {} on any failure (suggestions are best-effort)."""
    import requests as _rq
    wait = 1.0 - (time.time() - _last_api_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_api_call[0] = time.time()
    try:
        r = _rq.get(f"https://api.audible.com{path}", params=params, timeout=15,
                    headers={"User-Agent": "Plexify-Audiobooks/1.0"})
        if r.status_code == 200:
            return r.json()
    except Exception:
        log.warning("audible GET %s failed", path, exc_info=True)
    return {}


_RESPONSE_GROUPS = "contributors,product_desc,product_attrs"


def _product_dict(p: dict) -> Optional[dict]:
    if not p.get("asin") or not p.get("title"):
        return None
    # translations of books the user already owns dodge the title-dup filter (the title
    # differs by definition) — suggest English only
    if (p.get("language") or "english").lower() != "english":
        return None
    return {"asin": p["asin"], "title": (p.get("title") or "").strip(),
            "author": ((p.get("authors") or [{}])[0].get("name") or "").strip(),
            "runtime_min": p.get("runtime_length_min")}


# ── library snapshot + owned filtering ─────────────────────────────────────────────────────────

def library_snapshot(books_ledger_path: str) -> list:
    """[{asin, title, author, ts}] — latest organized record per dest whose file still exists."""
    latest: dict = {}
    try:
        with open(books_ledger_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("status") == "organized" and d.get("dest"):
                    latest[d["dest"]] = d
    except OSError:
        return []
    out = []
    for dest, d in latest.items():
        if not os.path.exists(dest):
            continue
        out.append({"asin": d.get("asin") or "", "title": d.get("title") or "",
                    "author": d.get("author") or "", "ts": d.get("ts") or ""})
    return out


def _title_key(s: str) -> str:
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", (s or "").lower())
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\b(the|a|an|unabridged|narrated by.*)\b", " ", s)
    return " ".join(s.split())


def filter_candidates(candidates: list, owned: list, wants: list, dismissed: list) -> list:
    """Drop books already owned (asin OR near-identical title+author), already wanted,
    or dismissed. Pure — unit-testable."""
    from rapidfuzz.fuzz import token_set_ratio, token_sort_ratio
    owned_asins = {b["asin"] for b in owned if b.get("asin")}
    taken_asins = owned_asins | {w.get("asin") for w in wants} | set(dismissed or [])
    owned_titles = [(_title_key(b["title"]), (b.get("author") or "").lower())
                    for b in owned if b.get("title")]
    out, seen = [], set()
    for c in candidates:
        if not c or c["asin"] in taken_asins or c["asin"] in seen:
            continue
        if (c.get("runtime_min") or 0) < 30:
            continue                          # shorts/samples aren't worth suggesting
        ck, ca = _title_key(c["title"]), (c.get("author") or "").lower()
        dup = False
        for ot, oa in owned_titles:
            ts = (token_set_ratio(ck, ot) + token_sort_ratio(ck, ot)) // 2
            if ts >= 80 and (not ca or not oa or token_set_ratio(ca, oa) >= 60):
                # 80 (not 85): edition/title variants of owned books — Sorcerer's vs
                # Philosopher's Stone — must count as duplicates when the author matches
                dup = True
                break
        if dup:
            continue
        seen.add(c["asin"])
        out.append(c)
    return out


def search_catalog(query: str, books_ledger_path: str = "", limit: int = 8) -> list:
    """Assistive/type-ahead search for the Audiobooks page: Audible catalog by keyword, returned
    FAST (no Soulseek gate — that would add ~15s and kill the type-ahead feel). Soulseek
    availability is checked separately/async via `availability()` so results appear the instant
    you type and each gets a '✓ on Soulseek' badge a moment later. Owned books are shown (the
    user explicitly searched); already-wanted / dismissed are hidden."""
    query = (query or "").strip()
    if len(query) < 1:
        return []
    d = _audible_get("/1.0/catalog/products",
                     {"num_results": str(max(limit * 2, 12)), "keywords": query,
                      "response_groups": _RESPONSE_GROUPS})
    cands = [x for x in (_product_dict(p) for p in d.get("products") or []) if x]
    taken = {w.get("asin") for w in load_wants()} | set(_load_json(DISMISSED_FILE, []))
    seen, out = set(), []
    for c in cands:
        if c["asin"] in taken or c["asin"] in seen:
            continue
        seen.add(c["asin"])
        c["reason"] = "search result"
        out.append(c)
    # a title that literally starts with the query floats up ('f' → 'Fahrenheit 451' before
    # a book that merely contains an f) — assistive-search intuition
    ql = query.lower()
    out.sort(key=lambda c: (not (c.get("title") or "").lower().startswith(ql),
                            not ql in (c.get("title") or "").lower()))
    return out[:limit]


def availability(items: list, max_workers: int = 6) -> dict:
    """{asin: bool} — is each item findable on Soulseek right now? Probed in parallel so the UI
    can badge results a moment after they appear. Best-effort: slskd down/unconfigured → {}
    (the UI just shows no badge; Download still queues + retries for days)."""
    from . import slskd_client
    items = [i for i in (items or []) if i.get("asin")][:20]
    if not items or not slskd_client.configured():
        return {}
    from concurrent.futures import ThreadPoolExecutor
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            pairs = list(ex.map(lambda it: (it["asin"], on_soulseek(it)), items))
    except Exception:
        log.warning("availability probe failed", exc_info=True)
        return {}
    return {a: ok for a, ok in pairs}


# ── the wanted list ────────────────────────────────────────────────────────────────────────────
# All wants-file writes go through _WANTS_FILE_LOCK. The worker's acquire_pass and the HTTP
# handler threads (add/remove) both mutate it; without the lock a want added mid-pass would be
# clobbered by the pass's save. The lock is held only across a read-modify-write (never across a
# network search), so UI writes never block on a 30s Soulseek probe.
_WANTS_FILE_LOCK = None


def _wants_lock():
    global _WANTS_FILE_LOCK
    if _WANTS_FILE_LOCK is None:
        import threading
        _WANTS_FILE_LOCK = threading.Lock()
    return _WANTS_FILE_LOCK


def _want_key(w: dict) -> str:
    return (w.get("asin") or "").strip() or (w.get("title") or "").strip().lower()


def load_wants() -> list:
    return _load_json(WANTS_FILE, [])


def save_wants(wants: list) -> None:
    _save_json(WANTS_FILE, wants)


def merge_save_wants(modified: list) -> None:
    """Save the pass's status changes without dropping wants added concurrently. acquire_pass
    only CHANGES status (never removes), so re-read the current file and overlay our per-key
    versions; anything added since our snapshot survives."""
    with _wants_lock():
        mod_by = {_want_key(w): w for w in modified}
        current = load_wants()
        merged = [mod_by.get(_want_key(w), w) for w in current]
        _save_json(WANTS_FILE, merged)


def add_want(item: dict) -> dict:
    with _wants_lock():
        wants = load_wants()
        if item.get("asin") and any(w.get("asin") == item.get("asin") for w in wants):
            return {"ok": True, "already": True}
        wants.append({"asin": item.get("asin") or "", "title": item.get("title") or "",
                      "author": item.get("author") or "",
                      "runtime_min": item.get("runtime_min"),
                      "reason": item.get("reason") or "",
                      "status": "wanted", "attempts": 0, "next_try_at": 0,
                      "added_at": time.time()})
        save_wants(wants)
    return {"ok": True}


def remove_want(asin: str, title: str = "") -> dict:
    """Drop a want; if it was mid-download, cancel just its transfers so they don't keep
    downloading unattended and clutter complete/."""
    from . import slskd_client
    with _wants_lock():
        wants = load_wants()
        gone = [w for w in wants
                if ((asin and w.get("asin") == asin)
                    or (not asin and title and w.get("title") == title))]
        keep = [w for w in wants if w not in gone]
        save_wants(keep)
    for w in gone:
        if w.get("status") == "downloading" and w.get("peer"):
            try:
                slskd_client.cancel_downloads(w["peer"], w.get("files") or [])
            except Exception:
                log.warning("cancel on unwant failed for %r", w.get("title"))
    return {"ok": True, "removed": len(gone)}


def dismiss(asin: str) -> dict:
    """Hide a book from search results (already-owned false-positives etc.)."""
    d = _load_json(DISMISSED_FILE, [])
    if asin and asin not in d:
        d.append(asin)
        _save_json(DISMISSED_FILE, d[-500:])
    return {"ok": True}


def apply_retry(want: dict, error: str = "") -> None:
    """Advance the backoff clock — mutates in place. Past the schedule end = gave_up."""
    n = int(want.get("attempts") or 0)
    if n >= len(_BACKOFF_HOURS):
        want["status"] = "gave_up"
        want["last_error"] = error or want.get("last_error") or "no acceptable result"
        return
    want["attempts"] = n + 1
    want["status"] = "wanted"
    want["next_try_at"] = time.time() + _BACKOFF_HOURS[n] * 3600
    if error:
        want["last_error"] = error


# ── soulseek: audiobook-shaped result picking ──────────────────────────────────────────────────

def _tokens(s: str) -> set:
    return set(re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split())


def pick_audiobook_dir(responses: list, title: str, author: str = "",
                       runtime_min: Optional[int] = None) -> Optional[dict]:
    """Choose the best (peer, directory) from slskd search responses for a BOOK — with guards
    tight enough that a MUSIC file whose path merely happens to contain the title words can't
    slip through (a 'Master Alvin' want once matched a Prodigy 'Alvin Risk Remix.m4a' — both
    'master' and 'alvin' were literally in that path). Returns {peer, dir, files, score} or None.

    Hard rejects: FLAC-dominant dir (music); a LONE .m4a (audiobooks are .m4b — a single .m4a
    is a music track); when the runtime is known, a total size outside a plausible band for it
    (~0.2–3.0 MB/min); and — the key one for title-word collisions — a match that lacks BOTH an
    author-name hit in the path AND a runtime-corroborated size (title tokens alone are never
    enough to accept)."""
    t_core = _tokens(title) - {"the", "a", "an", "of", "and"}
    if not t_core:
        return None
    a_tokens = _tokens(author) - {"the", "a", "an", "of", "and"}
    best = None
    for resp in responses or []:
        peer = resp.get("username") or ""
        by_dir: dict = {}
        for f in resp.get("files") or []:
            fn = f.get("filename") or ""
            d = fn.replace("\\", "/").rsplit("/", 1)[0]
            by_dir.setdefault(d, []).append(f)
        for d, files in by_dir.items():
            path_l = d.lower() + " " + " ".join(
                (f.get("filename") or "").rsplit("\\", 1)[-1].lower() for f in files[:4])
            if sum(1 for t in t_core if t in path_l) < max(1, int(len(t_core) * 0.6)):
                continue
            if _BOOK_JUNK_RE.search(path_l):
                continue
            exts = [os.path.splitext(f.get("filename") or "")[1].lower() for f in files]
            n_m4b = sum(1 for e in exts if e == ".m4b")
            n_m4a = sum(1 for e in exts if e == ".m4a")
            n_mp3 = sum(1 for e in exts if e == ".mp3")
            n_flac = sum(1 for e in exts if e == ".flac")
            audio = [f for f, e in zip(files, exts) if e in _AUDIO_EXTS]
            if not audio or n_flac > (n_m4b + n_m4a + n_mp3):
                continue                       # music share, not a book
            # a lone .m4a (no .m4b, no mp3 pile) is a music track, not an audiobook
            if n_m4a and not n_m4b and n_mp3 == 0 and len(audio) <= 2:
                continue
            total_mb = sum(int(f.get("size") or 0) for f in audio) / 1048576
            if total_mb < 25:
                continue                       # too small to be a full book

            author_hit = bool(a_tokens & _tokens(path_l))
            size_ok = None
            if runtime_min:
                lo, hi = runtime_min * 0.2, runtime_min * 3.0
                if not (lo <= total_mb <= hi):
                    continue                   # impossible size for this runtime → reject
                size_ok = runtime_min * 0.3 <= total_mb <= runtime_min * 2.0
            # THE collision guard: title-word overlap alone is not enough. Accept only with an
            # author-name hit in the path OR a runtime-corroborated size. (No author + no
            # runtime known = we can't tell it's the right book → skip.)
            if not author_hit and not size_ok:
                continue

            score = (30 if n_m4b else 0) + min(n_mp3, 20)
            if author_hit:
                score += 20
            if size_ok:
                score += 20
            score -= min(int(resp.get("queueLength") or 0), 10)   # long queues stall for hours
            if best is None or score > best["score"]:
                best = {"peer": peer, "dir": d, "files": audio, "score": score,
                        "total_mb": int(total_mb)}
    return best


def _search_queries(title: str, author: str) -> list:
    t = re.sub(r"\([^)]*\)", " ", title or "").strip()
    t = " ".join(t.split())
    out = []
    if author:
        out.append(f"{author} {t}")
    out.append(t)
    if author:
        out.append(f"{t} {author.split()[-1]}")
    seen, uniq = set(), []
    for q in out:
        if q.lower() not in seen:
            seen.add(q.lower())
            uniq.append(q)
    return uniq[:3]


def _slskd_search(title: str, author: str, runtime_min,
                  wait_seconds: float = 25.0) -> Optional[dict]:
    from . import slskd_client
    if not slskd_client.configured():
        return None
    for q in _search_queries(title, author):
        sid = slskd_client.search(q, timeout=8.0)
        if not sid:
            continue
        responses = slskd_client.get_search_results(sid, wait_seconds=wait_seconds)
        pick = pick_audiobook_dir(responses, title, author, runtime_min)
        if pick:
            return pick
    return None


# Soulseek search timing (measured live 2026-07-10): the peer holding the good m4b often
# responds ~28-30s in, so a 25s window MISSES available books; 30s catches them. slskd also
# returns HTTP 429 when searches are fired concurrently, so probes MUST be sequential + paced.
_PROBE_WINDOW_S = 30.0
_PROBE_GAP_S = 2.0
_PROBE_BUDGET_S = 360.0        # cap total probing so a daily regen can't run for 20 minutes


def on_soulseek(item: dict, wait_seconds: float = _PROBE_WINDOW_S) -> bool:
    """True iff a book-shaped result for this item exists on Soulseek RIGHT NOW — the guarantee
    that everything the UI shows is actually gettable. Best-effort: slskd unconfigured or a
    search error is treated as 'unknown' → False (don't show what we can't confirm)."""
    from . import slskd_client
    if not slskd_client.configured():
        return False
    try:
        title = item.get("title") or ""
        author = item.get("author") or ""
        q = _search_queries(title, author)[0]
        sid = slskd_client.search(q, timeout=8.0)
        if not sid:
            return False
        responses = slskd_client.get_search_results(sid, wait_seconds=wait_seconds)
        return pick_audiobook_dir(responses, title, author, item.get("runtime_min")) is not None
    except Exception:
        log.warning("on_soulseek check failed for %r", item.get("title"), exc_info=True)
        return False


def filter_on_soulseek(items: list, keep: int = 15, max_probes: int = 24) -> list:
    """Keep only items currently gettable on Soulseek, probed SEQUENTIALLY (parallel searches
    hit slskd's 429 rate limit → false negatives) with a small gap and a total-time budget.
    Stops at `keep` confirmed, `max_probes` attempts, or the budget — whichever first."""
    from . import slskd_client
    if not slskd_client.configured() or not items:
        return []
    confirmed, deadline = [], time.time() + _PROBE_BUDGET_S
    for i, it in enumerate(items[:max_probes]):
        if len(confirmed) >= keep or time.time() > deadline:
            break
        if i:
            time.sleep(_PROBE_GAP_S)
        if on_soulseek(it):
            confirmed.append(it)
    return confirmed


# ── the acquire pass (driven from the daemon worker) ───────────────────────────────────────────

_ACQUIRE_LOCK = None


def _acquire_lock():
    global _ACQUIRE_LOCK
    if _ACQUIRE_LOCK is None:
        import threading
        _ACQUIRE_LOCK = threading.Lock()
    return _ACQUIRE_LOCK


def acquire_pass(import_dir: str) -> dict:
    """Drive every due want one step. Serial and bounded: at most ONE new search per pass
    (Soulseek etiquette), but all in-flight downloads get their transfer check.

    Single-flight (worker vs organize-now can both call this) AND the whole read-modify-write
    is under a lock — otherwise a UI add_want/remove_want between load and save silently
    vanishes. If slskd is unreachable, the pass returns early WITHOUT touching wants so a
    weekend outage doesn't burn every book's retries down to gave_up."""
    out = {"searched": 0, "started": 0, "delivered": 0, "retried": 0, "gave_up": 0}
    from . import slskd_client
    if not slskd_client.configured():
        out["skipped"] = "slskd not configured"
        return out
    if not _acquire_lock().acquire(blocking=False):
        out["skipped"] = "already running"
        return out
    try:
        return _acquire_pass_locked(import_dir, out)
    finally:
        _acquire_lock().release()


def _acquire_pass_locked(import_dir: str, out: dict) -> dict:
    from . import slskd_client
    wants = load_wants()
    if not wants:
        return out
    now = time.time()
    dirty = False

    for w in wants:
        if w.get("status") != "downloading":
            continue
        peer = w.get("peer") or ""
        names = set(w.get("files") or [])
        started = w.get("download_started_at") or now
        transfers = slskd_client.get_transfers_for_user(peer)
        ours = [t for t in transfers if (t.get("filename") or "") in names]
        states = [(t.get("state") or "") for t in ours]
        # 'Completed, TimedOut'/'Aborted' are FAILURES too — only Succeeded is success
        all_done = bool(ours) and len(ours) == len(names) and all("Completed" in s for s in states)
        any_bad = any(k in s for s in states
                      for k in ("Errored", "Cancelled", "Rejected", "TimedOut", "Aborted", "Failed"))
        if all_done and not any_bad:
            delivered = _collect_delivered(w, import_dir)
            if delivered:
                w["status"] = "delivered"
                w["delivered_at"] = now
                w["delivered_files"] = delivered
                out["delivered"] += 1
                dirty = True
            elif now - started > _DOWNLOAD_TIMEOUT_S:
                apply_retry(w, "completed but files never appeared")
                out["retried"] += 1
                dirty = True
            # else: files still moving incomplete/->complete/ — wait for next pass
        elif all_done and any_bad:
            slskd_client.cancel_downloads(peer, names)   # scoped — never the peer's music
            apply_retry(w, "peer failed mid-download")
            out["retried"] += 1
            dirty = True
        elif now - started > _DOWNLOAD_TIMEOUT_S:
            slskd_client.cancel_downloads(peer, names)
            apply_retry(w, "download timed out")
            out["retried"] += 1
            dirty = True
        # else: still transferring — leave it alone

    searched_this_pass = False
    for w in wants:
        if w.get("status") != "wanted" or (w.get("next_try_at") or 0) > now:
            continue
        if searched_this_pass:
            break
        searched_this_pass = True
        out["searched"] += 1
        try:
            pick = _slskd_search(w.get("title") or "", w.get("author") or "",
                                 w.get("runtime_min"))
        except Exception:
            log.exception("audiobook want search failed for %r", w.get("title"))
            pick = None
        if not pick:
            apply_retry(w, "no acceptable result on soulseek")
            out["gave_up" if w["status"] == "gave_up" else "retried"] += 1
            dirty = True
            continue
        queued = 0
        for f in pick["files"]:
            if slskd_client.enqueue_download(pick["peer"], f.get("filename") or "",
                                             int(f.get("size") or 0)):
                queued += 1
        if queued == len(pick["files"]):
            w["status"] = "downloading"
            w["peer"] = pick["peer"]
            w["files"] = [f.get("filename") for f in pick["files"]]
            w["download_started_at"] = now
            w["total_mb"] = pick.get("total_mb")
            out["started"] += 1
        else:
            slskd_client.cancel_downloads(pick["peer"],
                                          [f.get("filename") for f in pick["files"]])
            apply_retry(w, "peer rejected part of the queue")
            out["retried"] += 1
        dirty = True

    if dirty:
        merge_save_wants(wants)
    if any(v for k, v in out.items() if k != "skipped"):
        log.info("audiobook acquire_pass: %s", out)
    return out


# Only slskd's COMPLETE dirs — never the raw download root (that includes incomplete/ and,
# on a shared instance, the music daemon's staging), which is how a bare-basename match could
# steal a music file or grab a still-writing partial.
_DELIVERY_ROOTS = ["/downloads_music/complete", "/downloads/music/complete"]


def _suffix2(path: str) -> str:
    """Last two path components lowercased: '<parentdir>/<basename>'. Far more specific than
    a bare basename — two different books both containing '01.mp3' won't collide because their
    parent folders differ."""
    parts = [p for p in (path or "").replace("\\", "/").split("/") if p]
    return "/".join(parts[-2:]).lower() if len(parts) >= 2 else (parts[-1].lower() if parts else "")


def _collect_delivered(want: dict, import_dir: str, roots: Optional[list] = None) -> list:
    """Move THIS book's completed files into one import folder — but only when ALL of them are
    present (a partial set means slskd is still moving files; wait). Matches on the
    parent-dir+basename suffix so a generic '01.mp3' can't be stolen from another book/music."""
    want_files = [f for f in (want.get("files") or []) if f]
    if not want_files:
        return []
    wanted = {_suffix2(f) for f in want_files}
    roots = roots or _DELIVERY_ROOTS
    match: dict = {}                      # suffix -> abs path
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dp, dns, fns in os.walk(root):
            for fn in fns:
                s = _suffix2(os.path.join(dp, fn))
                if s in wanted and s not in match:
                    match[s] = os.path.join(dp, fn)
    if len(match) < len(wanted):
        return []                          # not all files landed yet — not ready
    book = f"{want.get('author') or 'Unknown'} - {want.get('title') or 'Book'}"
    book = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", book).strip(". ") or "Book"
    dst_dir = os.path.join(import_dir, book)
    n = 0
    while os.path.exists(dst_dir):
        n += 1
        dst_dir = os.path.join(import_dir, f"{book}_{n}")
    os.makedirs(dst_dir)
    moved = []
    for p in match.values():
        try:
            shutil.move(p, os.path.join(dst_dir, os.path.basename(p)))
            moved.append(os.path.basename(p))
        except OSError:
            log.exception("delivered move failed for %s", p)
    return moved


def wanted_status() -> list:
    """The wants as the UI shows them, most recent first."""
    now = time.time()
    out = []
    for w in sorted(load_wants(), key=lambda w: -(w.get("added_at") or 0)):
        v = {k: w.get(k) for k in ("asin", "title", "author", "status", "attempts",
                                   "reason", "last_error", "total_mb", "runtime_min")}
        nt = w.get("next_try_at") or 0
        v["next_try_in_s"] = max(0, int(nt - now)) if w.get("status") == "wanted" and nt else 0
        out.append(v)
    return out
