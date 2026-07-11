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

Soulseek access reuses slskd_client verbatim (search / get_search_results /
enqueue_download / get_transfers_for_user / cancel_downloads_for_user). Result picking is
audiobook-shaped: a directory with one m4b or a pile of mp3s whose path matches the title,
sized plausibly for the book's runtime — never FLAC-dominant dirs (that's music).
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
SUGGESTIONS_FILE = "audiobook_suggestions.json"
WANTS_FILE = "audiobook_wants.json"
DISMISSED_FILE = "audiobook_dismissed.json"

_SUGGEST_TTL_S = 24 * 3600
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


def author_products(author: str, n: int = 25) -> list:
    d = _audible_get("/1.0/catalog/products",
                     {"num_results": str(n), "author": author,
                      "response_groups": _RESPONSE_GROUPS,
                      "products_sort_by": "-ReleaseDate"})
    return [x for x in (_product_dict(p) for p in d.get("products") or []) if x]


def similar_products(asin: str, n: int = 10) -> list:
    d = _audible_get(f"/1.0/catalog/products/{asin}/sims",
                     {"num_results": str(n), "response_groups": _RESPONSE_GROUPS})
    return [x for x in (_product_dict(p) for p in d.get("similar_products") or []) if x]


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


def generate_suggestions(books_ledger_path: str, limit: int = 30,
                         max_authors: int = 8, sims_seeds: int = 10) -> list:
    """The suggestor: author gaps first (you clearly like them), then sims ranked by how many
    owned books point at them. Best-effort; every API failure just shrinks the list."""
    owned = library_snapshot(books_ledger_path)
    if not owned:
        return []
    wants = load_wants()
    dismissed = _load_json(DISMISSED_FILE, [])

    by_author: dict = {}
    for b in owned:
        a = (b.get("author") or "").strip()
        if a:
            by_author.setdefault(a, []).append(b)
    top_authors = sorted(by_author, key=lambda a: -len(by_author[a]))[:max_authors]

    author_sugs = []
    for a in top_authors:
        cands = filter_candidates(author_products(a), owned, wants, dismissed)
        for c in cands[:4]:
            c["reason"] = f"more by {a}"
            author_sugs.append(c)

    seeds = sorted((b for b in owned if b.get("asin")),
                   key=lambda b: b.get("ts") or "", reverse=True)[:sims_seeds]
    votes: dict = {}
    for b in seeds:
        for c in similar_products(b["asin"], 8):
            slot = votes.setdefault(c["asin"], {"item": c, "n": 0, "because": b["title"]})
            slot["n"] += 1
    sims = [dict(v["item"], reason=f"because you have {v['because']}")
            for v in sorted(votes.values(), key=lambda v: -v["n"])]
    sims = filter_candidates(sims, owned, wants, dismissed)
    # keep reasons attached post-filter
    reason_by_asin = {v["item"]["asin"]: f"because you have {v['because']}"
                      for v in votes.values()}
    for c in sims:
        c["reason"] = reason_by_asin.get(c["asin"], "similar to your library")

    combined, seen = [], set()
    for c in author_sugs + sims:
        if c["asin"] not in seen:
            seen.add(c["asin"])
            combined.append(c)
    return combined[:limit]


def suggestions_cached(books_ledger_path: str, force: bool = False) -> dict:
    """{ts, items} — regenerated when stale (daily), forced, or missing."""
    cache = _load_json(SUGGESTIONS_FILE, {})
    if not force and cache.get("items") is not None \
            and time.time() - (cache.get("ts") or 0) < _SUGGEST_TTL_S:
        return cache
    items = generate_suggestions(books_ledger_path)
    cache = {"ts": time.time(), "items": items}
    _save_json(SUGGESTIONS_FILE, cache)
    return cache


# ── the wanted list ────────────────────────────────────────────────────────────────────────────

def load_wants() -> list:
    return _load_json(WANTS_FILE, [])


def save_wants(wants: list) -> None:
    _save_json(WANTS_FILE, wants)


def add_want(item: dict) -> dict:
    wants = load_wants()
    if any(w.get("asin") == item.get("asin") for w in wants if item.get("asin")):
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
    wants = load_wants()
    keep = [w for w in wants
            if not ((asin and w.get("asin") == asin)
                    or (not asin and title and w.get("title") == title))]
    save_wants(keep)
    return {"ok": True, "removed": len(wants) - len(keep)}


def dismiss(asin: str) -> dict:
    d = _load_json(DISMISSED_FILE, [])
    if asin and asin not in d:
        d.append(asin)
        _save_json(DISMISSED_FILE, d[-500:])
    cache = _load_json(SUGGESTIONS_FILE, {})
    if cache.get("items"):
        cache["items"] = [i for i in cache["items"] if i.get("asin") != asin]
        _save_json(SUGGESTIONS_FILE, cache)
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
    """Choose the best (peer, directory) from slskd search responses for a BOOK:
    the title's tokens must appear in the directory path, the dir must be one m4b or a
    pile of mp3s (FLAC-dominant = music, skip), and when the runtime is known the total
    size has to be plausible for it. Returns {peer, dir, files:[{filename,size}], score}."""
    t_tokens = _tokens(title)
    stop = {"the", "a", "an", "of", "and"}
    t_core = t_tokens - stop
    if not t_core:
        return None
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
                (f.get("filename") or "").rsplit("\\", 1)[-1].lower() for f in files[:3])
            hit = sum(1 for t in t_core if t in path_l)
            if hit < max(1, int(len(t_core) * 0.6)):
                continue
            if _BOOK_JUNK_RE.search(path_l):
                continue
            exts = [os.path.splitext(f.get("filename") or "")[1].lower() for f in files]
            n_m4b = sum(1 for e in exts if e in (".m4b", ".m4a"))
            n_mp3 = sum(1 for e in exts if e == ".mp3")
            n_flac = sum(1 for e in exts if e == ".flac")
            audio = [f for f, e in zip(files, exts) if e in _AUDIO_EXTS]
            if not audio or n_flac > (n_m4b + n_mp3):
                continue                       # music share, not a book
            total_mb = sum(int(f.get("size") or 0) for f in audio) / 1048576
            if total_mb < 25:
                continue                       # too small to be a full book
            score = hit * 10 + (30 if n_m4b else 0) + min(n_mp3, 20)
            if author and _tokens(author) & _tokens(path_l):
                score += 15
            if runtime_min:
                lo, hi = runtime_min * 0.3, runtime_min * 2.0
                if lo <= total_mb <= hi:
                    score += 20
                elif total_mb < lo * 0.5 or total_mb > hi * 2:
                    score -= 25
            slot = resp.get("queueLength") or 0
            score -= min(int(slot), 10)        # long peer queues stall for hours
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


def _slskd_search(title: str, author: str, runtime_min) -> Optional[dict]:
    from . import slskd_client
    if not slskd_client.configured():
        return None
    for q in _search_queries(title, author):
        sid = slskd_client.search(q, timeout=8.0)
        if not sid:
            continue
        responses = slskd_client.get_search_results(sid, wait_seconds=25.0)
        pick = pick_audiobook_dir(responses, title, author, runtime_min)
        if pick:
            return pick
    return None


# ── the acquire pass (driven from the daemon worker) ───────────────────────────────────────────

def acquire_pass(import_dir: str) -> dict:
    """Drive every due want one step. Serial and bounded: at most ONE new search per pass
    (Soulseek etiquette), but all in-flight downloads get their transfer check."""
    out = {"searched": 0, "started": 0, "delivered": 0, "retried": 0, "gave_up": 0}
    wants = load_wants()
    if not wants:
        return out
    from . import slskd_client
    now = time.time()
    dirty = False

    for w in wants:
        if w.get("status") != "downloading":
            continue
        peer = w.get("peer") or ""
        started = w.get("download_started_at") or now
        transfers = slskd_client.get_transfers_for_user(peer)
        names = set(w.get("files") or [])
        ours = [t for t in transfers if (t.get("filename") or "") in names]
        states = [(t.get("state") or "") for t in ours]
        if ours and all("Completed" in s for s in states):
            if any("Errored" in s or "Cancelled" in s or "Rejected" in s for s in states):
                slskd_client.cancel_downloads_for_user(peer)
                apply_retry(w, "peer failed mid-download")
                out["retried"] += 1
            else:
                delivered = _collect_delivered(w, import_dir)
                if delivered:
                    w["status"] = "delivered"
                    w["delivered_at"] = now
                    w["delivered_files"] = delivered
                    out["delivered"] += 1
                else:
                    apply_retry(w, "downloads completed but files not found")
                    out["retried"] += 1
            dirty = True
        elif now - started > _DOWNLOAD_TIMEOUT_S:
            slskd_client.cancel_downloads_for_user(peer)
            apply_retry(w, "download timed out")
            out["retried"] += 1
            dirty = True
        # else: still transferring (or transfer list momentarily empty) — leave it alone

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
            out["retried" if w["status"] == "wanted" else "gave_up"] += 1
            dirty = True
            continue
        queued = 0
        for f in pick["files"]:
            if slskd_client.enqueue_download(pick["peer"], f.get("filename") or "",
                                             int(f.get("size") or 0)):
                queued += 1
        if queued:
            w["status"] = "downloading"
            w["peer"] = pick["peer"]
            w["files"] = [f.get("filename") for f in pick["files"]]
            w["download_started_at"] = now
            w["total_mb"] = pick.get("total_mb")
            out["started"] += 1
        else:
            apply_retry(w, "peer rejected the queue")
            out["retried"] += 1
        dirty = True

    if dirty:
        save_wants(wants)
    if any(out.values()):
        log.info("audiobook acquire_pass: %s", out)
    return out


_DELIVERY_ROOTS = ["/downloads_music/complete", "/downloads_music",
                   "/downloads/music/complete", "/downloads/music"]


def _collect_delivered(want: dict, import_dir: str, roots: Optional[list] = None) -> list:
    """Find the completed files in slskd's download tree and move them into ONE folder in
    the unified import dir — the router takes it from there."""
    basenames = {(f or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
                 for f in (want.get("files") or [])}
    roots = roots or _DELIVERY_ROOTS
    found = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dp, dns, fns in os.walk(root):
            for fn in fns:
                if fn.lower() in basenames:
                    found.append(os.path.join(dp, fn))
        if found:
            break
    if not found:
        return []
    book = f"{want.get('author') or 'Unknown'} - {want.get('title') or 'Book'}"
    book = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", book).strip(". ")
    dst_dir = os.path.join(import_dir, book)
    n = 0
    while os.path.exists(dst_dir):
        n += 1
        dst_dir = os.path.join(import_dir, f"{book}_{n}")
    os.makedirs(dst_dir)
    moved = []
    for p in found:
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
