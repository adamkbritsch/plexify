"""audiobook_client.py — the engine's view of the daemon-side audiobook organizer.

The organizer itself runs inside the plexify-downloader daemon (near storage — m4b tag writes
rewrite whole files). This module is the Mac/engine glue: proxy status, push the user's settings
into the daemon's own config DB, fire organize-now, resolve review items, and — the engine-only
responsibility — trigger a Plex section scan when new books have been organized (the daemon never
talks to Plex, same division of labor as music).

Transport reuses nas_downloader._req (host fallback + bearer token). In single-host deployments
the "daemon" is just the sibling container, so everything here works unchanged.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _req(path: str, payload: dict | None = None, timeout: int = 15):
    from .nas_downloader import _req as _nreq
    return _nreq(path, payload, timeout=timeout)


def daemon_status() -> dict:
    """GET /audiobooks/status — {ok, enabled, dirs_ok, dropped, converting, untagged, review,
    organized_total, recent, library_visible} or {reachable: False, error}."""
    try:
        out = _req("/audiobooks/status")
        out["reachable"] = True
        return out
    except Exception as e:
        return {"reachable": False, "error": str(e)[:200]}


def push_config() -> bool:
    """Forward the engine's audiobook settings into the daemon's own config DB (it starts fresh;
    the settings UI only writes the engine's DB — same precedent as the source-config forward)."""
    try:
        from .db import get_config
        body = {
            "audiobook_enabled": get_config("audiobook_enabled", "0") or "0",
            "audiobook_min_confidence": get_config("audiobook_min_confidence", "80") or "80",
        }
        res = _req("/audiobooks/config", body)
        return bool(res.get("ok"))
    except Exception:
        log.warning("audiobook push_config failed", exc_info=True)
        return False


def organize_now() -> dict:
    try:
        return _req("/audiobooks/organize-now", {})
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def resolve(file: str, asin: str | None = None, author: str | None = None,
            title: str | None = None) -> dict:
    try:
        return _req("/audiobooks/resolve",
                    {"file": file, "asin": asin, "author": author, "title": title}, timeout=60)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


_CLEANUP_BUSY = [False]


def _cleanup_pending() -> list:
    from .db import get_config
    import json as _json
    try:
        return _json.loads(get_config("audiobook_cleanup_pending", "[]") or "[]")
    except ValueError:
        return []


def _set_cleanup_pending(items: list) -> None:
    from .db import set_config
    import json as _json
    set_config("audiobook_cleanup_pending", _json.dumps(items[:20]))


def _run_cleanup(rel: str) -> None:
    """One Plex-cleanup attempt for a deleted book; a missed window stays in the pending
    list and audiobook_tick keeps retrying (bounded) — one-shot cleanup raced slow scans."""
    try:
        from . import plex_client
        res = plex_client.cleanup_deleted_album(rel)
        if res.get("cleared"):
            _set_cleanup_pending([p for p in _cleanup_pending() if p.get("rel") != rel])
    except Exception:
        log.exception("plex cleanup after delete failed")


def delete_book(rel_dir: str = "", dest: str = "") -> dict:
    """Soft-delete via the daemon (book folder → in-library trash), then clean the Plex entry
    up in the background — scan + guarded trash-flag sweep (never media/metadata deletion).
    The book's rel_dir goes on a pending list so the tick retries until Plex is clean."""
    try:
        res = _req("/audiobooks/delete", {"rel_dir": rel_dir, "dest": dest}, timeout=60)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    if res.get("ok"):
        import threading
        rel = res.get("rel_dir") or rel_dir            # daemon returns the canonical form
        if rel:
            pending = [p for p in _cleanup_pending() if p.get("rel") != rel]
            pending.append({"rel": rel, "tries": 0})
            _set_cleanup_pending(pending)
            def _first(r=rel):
                if _CLEANUP_BUSY[0]:
                    return          # a cleanup is running — the tick retry covers this one
                _CLEANUP_BUSY[0] = True
                try:
                    _run_cleanup(r)
                finally:
                    _CLEANUP_BUSY[0] = False
            threading.Thread(target=_first, daemon=True, name="ab-delete-cleanup").start()
    return res


def discard_review(file: str) -> dict:
    try:
        return _req("/audiobooks/discard", {"file": file}, timeout=60)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def suggestions(refresh: bool = False) -> dict:
    try:
        return _req("/audiobooks/suggestions" + ("?refresh=1" if refresh else ""),
                    timeout=90 if refresh else 20)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "items": []}


def search_books(query: str) -> dict:
    import urllib.parse
    try:
        return _req("/audiobooks/search?q=" + urllib.parse.quote(query or ""), timeout=120)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "items": []}


def wanted() -> dict:
    try:
        return _req("/audiobooks/wanted", timeout=15)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "items": []}


def want(item: dict) -> dict:
    try:
        return _req("/audiobooks/want", item, timeout=15)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def unwant(asin: str = "", title: str = "") -> dict:
    try:
        return _req("/audiobooks/unwant", {"asin": asin, "title": title}, timeout=15)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def dismiss_suggestion(asin: str) -> dict:
    try:
        return _req("/audiobooks/dismiss", {"asin": asin}, timeout=15)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def audiobook_tick() -> dict:
    """Engine-side glue, safe to call often (status poll / scheduler): push config, read daemon
    status, and when the organized count has grown since the last look, trigger a Plex scan of
    the audiobook section so new books appear without user action."""
    out = {"pushed": False, "scan_triggered": False}
    try:
        from .db import get_config, set_config
        if (get_config("audiobook_enabled", "0") or "0") != "1":
            out["skipped"] = "disabled"
            return out
        out["pushed"] = push_config()
        st = daemon_status()
        out["status"] = {k: st.get(k) for k in ("reachable", "enabled", "dirs_ok",
                                                "untagged", "review", "organized_total")}
        if not st.get("reachable"):
            return out
        total = int(st.get("organized_total") or 0)
        prev = int(get_config("audiobook_last_organized_count", "0") or "0")
        if total > prev:
            try:
                from . import plex_client
                if plex_client.trigger_audiobook_scan():
                    out["scan_triggered"] = True
            except Exception:
                log.exception("audiobook_tick: plex scan trigger failed")
            set_config("audiobook_last_organized_count", str(total))
            # The scan is async and multi-part books can split across scans (per-part agent
            # matches, duplicate local artists) — flag a reconcile for the FOLLOWING ticks,
            # when the scan has had time to land.
            set_config("audiobook_reconcile_pending", "1")
        # retry Plex cleanup for deleted books whose scan/emptyTrash window was missed —
        # threaded (the cleanup polls the scan for up to 90s; the tick must stay fast),
        # single-flight, bounded per book: entries clear on success or after 5 tries
        pending = _cleanup_pending()
        if pending and not _CLEANUP_BUSY[0]:
            item = pending[0]
            if item.get("tries", 0) >= 5:
                _set_cleanup_pending(pending[1:])
                log.warning("giving up Plex cleanup for deleted %s after 5 tries", item.get("rel"))
            else:
                item["tries"] = item.get("tries", 0) + 1
                _set_cleanup_pending(pending)
                _CLEANUP_BUSY[0] = True
                import threading
                def _retry(rel=item.get("rel") or ""):
                    try:
                        _run_cleanup(rel)
                    finally:
                        _CLEANUP_BUSY[0] = False
                threading.Thread(target=_retry, daemon=True, name="ab-cleanup-retry").start()
                out["cleanup_retry"] = item.get("rel")
        if not (total > prev) and \
                (get_config("audiobook_reconcile_pending", "0") or "0") == "1":
            try:
                from . import plex_client
                rec = plex_client.reconcile_audiobook_albums()
                out["reconcile"] = rec
                if not any(rec.values()):
                    # a pass that changed nothing means the section has settled clean
                    set_config("audiobook_reconcile_pending", "0")
            except Exception:
                log.exception("audiobook_tick: reconcile failed")
    except Exception:
        log.exception("audiobook_tick failed")
    return out
