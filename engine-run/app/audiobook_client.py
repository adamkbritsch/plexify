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
    except Exception:
        log.exception("audiobook_tick failed")
    return out
