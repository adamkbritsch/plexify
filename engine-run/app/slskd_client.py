"""slskd_client.py — thin REST wrapper around the slskd API for Plexify.

All functions are synchronous (requests). None raise on HTTP errors —
they return empty lists / None / False on failure and log at WARNING level.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

# ── defaults (overridable via AppConfig) ─────────────────────────────────────
_DEFAULT_URL = os.environ.get("SLSKD_URL", "")  # set in Settings -> Connections (or SLSKD_URL env)
_DEFAULT_API_KEY = os.environ.get("SLSKD_API_KEY", "")  # set in Settings -> Connections (or SLSKD_API_KEY env)

_CONNECT_TIMEOUT = 5   # seconds for TCP connect
_READ_TIMEOUT    = 10  # seconds for response body


# ── config helpers ────────────────────────────────────────────────────────────

def _url() -> str:
    try:
        from .db import get_config, set_config
        val = get_config("slskd_url")
        if not val:
            set_config("slskd_url", _DEFAULT_URL)
            val = _DEFAULT_URL
        return val.rstrip("/")
    except Exception:
        return _DEFAULT_URL


def _api_key() -> str:
    try:
        from .db import get_config, set_config
        val = get_config("slskd_api_key")
        if not val:
            set_config("slskd_api_key", _DEFAULT_API_KEY)
            val = _DEFAULT_API_KEY
        return val
    except Exception:
        return _DEFAULT_API_KEY


def _headers() -> dict:
    return {"X-API-Key": _api_key(), "Content-Type": "application/json"}


# ── public API ────────────────────────────────────────────────────────────────

def configured() -> bool:
    """True if slskd URL + API key are set (even defaults count)."""
    try:
        return bool(_url() and _api_key())
    except Exception:
        return False


class _StatusObject:
    __slots__ = ("logged_in", "username")

    def __init__(self, logged_in: bool, username: Optional[str] = None):
        self.logged_in = logged_in
        self.username = username

    def __repr__(self):
        return f"_StatusObject(logged_in={self.logged_in!r}, username={self.username!r})"


def check_status() -> Optional[_StatusObject]:
    """Returns a small object with attrs: logged_in (bool), username (str|None).
    Returns None if not configured. Returns object with logged_in=False on connection errors.
    """
    if not configured():
        return None
    try:
        r = requests.get(
            f"{_url()}/api/v0/application",
            headers=_headers(),
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        if r.status_code == 401:
            log.warning("slskd_client: 401 from /application — check API key")
            return _StatusObject(logged_in=False)
        r.raise_for_status()
        data = r.json()
        srv = data.get("server") or {}
        logged_in = bool(srv.get("isLoggedIn"))
        username = (data.get("user") or {}).get("username")
        return _StatusObject(logged_in=logged_in, username=username)
    except requests.exceptions.ConnectionError as e:
        log.warning("slskd_client: connection error checking status: %s", e)
        return _StatusObject(logged_in=False)
    except Exception as e:
        log.warning("slskd_client: check_status failed: %s", e)
        return _StatusObject(logged_in=False)


def search(query: str, timeout: float = 8.0) -> str:
    """Initiate a search via POST /api/v0/searches. Returns search_id (str).
    Returns empty string on failure.
    """
    try:
        r = requests.post(
            f"{_url()}/api/v0/searches",
            headers=_headers(),
            json={"searchText": query},
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        if not r.ok:
            log.warning("slskd_client: search POST failed %d for %r", r.status_code, query)
            return ""
        return r.json().get("id", "")
    except Exception as e:
        log.warning("slskd_client: search(%r) raised: %s", query, e)
        return ""


def get_search_results(search_id: str, wait_seconds: float = 100.0) -> list[dict]:
    """Poll the search results until isComplete or until wait_seconds elapsed.
    Returns a list of response dicts (one per peer that responded).
    Always includes .
    """
    if not search_id:
        return []
    # L-7: retry transient errors with exponential backoff up to wait_seconds.
    deadline = time.monotonic() + wait_seconds
    interval = 1.5
    consec_fail = 0
    while True:
        try:
            r = requests.get(
                f"{_url()}/api/v0/searches/{search_id}",
                headers=_headers(),
                params={"includeResponses": "true"},
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            )
            if r.status_code in (502, 503, 504, 408, 429):
                consec_fail += 1
                log.info("slskd_client: search %s transient %d (retry #%d)", search_id, r.status_code, consec_fail)
                if consec_fail >= 5 or time.monotonic() >= deadline:
                    return []
            elif not r.ok:
                log.warning("slskd_client: get_search_results %d for id=%s", r.status_code, search_id)
                return []
            else:
                consec_fail = 0
                data = r.json()
                if data.get("isComplete") or time.monotonic() >= deadline:
                    return data.get("responses") or []
        except (requests.exceptions.RequestException, ValueError) as e:
            consec_fail += 1
            log.info("slskd_client: search %s transient %s (retry #%d)", search_id, type(e).__name__, consec_fail)
            if consec_fail >= 5 or time.monotonic() >= deadline:
                log.warning("slskd_client: get_search_results gave up after %d consecutive failures: %s", consec_fail, e)
                return []
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return []
        time.sleep(min(interval, remaining))
        interval = min(interval * 1.5, 8.0)


def browse_user(username: str) -> list[dict]:
    """GET /api/v0/users/{username}/browse — returns the peer\'s full library structure.
    Each entry has \'dirname\' and \'files\' keys. Returns [] on failure.
    """
    if not username:
        return []
    try:
        r = requests.get(
            f"{_url()}/api/v0/users/{username}/browse",
            headers=_headers(),
            timeout=(_CONNECT_TIMEOUT, 30),  # browse can be slow
        )
        if not r.ok:
            log.warning("slskd_client: browse_user %r → %d", username, r.status_code)
            return []
        return r.json() or []
    except Exception as e:
        log.warning("slskd_client: browse_user(%r) raised: %s", username, e)
        return []


def enqueue_download(username: str, filename: str, size: int = 0) -> bool:
    """POST /api/v0/transfers/downloads/{username} with a single file dict.
    Returns True on 200/201/204, False otherwise.
    """
    if not username or not filename:
        return False
    payload = [{"filename": filename, "size": size}]
    try:
        r = requests.post(
            f"{_url()}/api/v0/transfers/downloads/{username}",
            headers=_headers(),
            json=payload,
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        if r.status_code in (200, 201, 204):
            return True
        log.warning("slskd_client: enqueue_download %r %r → %d %s",
                    username, filename[-60:], r.status_code, r.text[:100])
        return False
    except Exception as e:
        log.warning("slskd_client: enqueue_download raised: %s", e)
        return False


def get_transfers_for_user(username: str) -> list[dict]:
    """GET /api/v0/transfers/downloads — filter to just the named user.
    Returns flat list of file-level transfer dicts.
    """
    if not username:
        return []
    try:
        r = requests.get(
            f"{_url()}/api/v0/transfers/downloads",
            headers=_headers(),
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        if not r.ok:
            log.warning("slskd_client: get_transfers_for_user → %d", r.status_code)
            return []
        all_users = r.json() or []
        files: list[dict] = []
        for entry in all_users:
            if entry.get("username", "").lower() != username.lower():
                continue
            for directory in entry.get("directories") or []:
                files.extend(directory.get("files") or [])
        return files
    except Exception as e:
        log.warning("slskd_client: get_transfers_for_user raised: %s", e)
        return []


def check_health() -> dict:
    """Returns {connected, logged_in, username, error}."""
    try:
        st = check_status()
        if st is None:
            return {"connected": False, "logged_in": False, "username": None,
                    "error": "not configured"}
        return {
            "connected": st.logged_in,
            "logged_in": st.logged_in,
            "username": st.username,
            "error": None,
        }
    except Exception as e:
        return {"connected": False, "logged_in": False, "username": None, "error": str(e)}
