"""Mac-side client for the NAS plexify-downloader daemon (:8788).

The split: the Mac engine decides WHAT to acquire (source order, retries,
placement); the NAS daemon executes ONE source attempt near storage (download +
fLaC/ffmpeg verification) and dumps verified files into its staging dir. This
client enqueues a job, polls it, then moves the delivered files from the
SMB-visible staging dir into the dest_dir the caller asked for — so every
adapter keeps its exact signature + return shape and picker_tick is untouched.

Daemon unreachable / job failed / timeout all return the adapter's normal
failure shape — the picker treats it like any failed source attempt.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

# Downloader daemon host(s), tried in order. Override per-setup with the
# NAS_DOWNLOADER_HOSTS env var (";"-separated) or the `nas_downloader_url` config key.
# Default assumes the daemon runs on the same host (Docker service name `plexify-downloader`).
_HOSTS = [h for h in os.environ.get(
    "NAS_DOWNLOADER_HOSTS", "http://plexify-downloader:8788").split(";") if h.strip()]
# Daemon's view of staging -> the org host's view of the same dir (SMB mount / bind mount).
_NAS_STAGING_PREFIX = "/downloads_music/staging"
_MAC_STAGING_PREFIX = os.environ.get(
    "STAGING_MOUNT", "/Volumes/MediaVolume3/Downloads/music/staging")
_POLL_SECS = 3
_QUEUE_GRACE = 240   # extra wait on top of the source's own timeout (serial daemon queue)


def _base_urls() -> list:
    try:
        from .db import get_config
        u = (get_config("nas_downloader_url", "") or "").rstrip("/")
        if u:
            return [u]
    except Exception:
        pass
    return _HOSTS


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    try:
        from .db import get_config
        tok = get_config("nas_downloader_token", "") or ""
        if tok:
            h["Authorization"] = "Bearer " + tok
    except Exception:
        pass
    return h


def _req(path: str, payload: dict | None = None, timeout: int = 10):
    last = None
    for base in _base_urls():
        try:
            req = urllib.request.Request(
                base + path,
                data=(json.dumps(payload).encode() if payload is not None else None),
                headers=_headers(),
                method="POST" if payload is not None else "GET",
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001 — any transport error -> try next host
            last = e
    raise last or OSError("no downloader host reachable")


def _to_mac_path(p: str) -> str:
    if p.startswith(_NAS_STAGING_PREFIX):
        return _MAC_STAGING_PREFIX + p[len(_NAS_STAGING_PREFIX):]
    return p


def _collect(job: dict, dest_dir: str | None) -> list:
    """Move delivered files from the SMB staging dir into dest_dir (same share =
    server-side rename, cheap). Falls back to returning staging paths."""
    paths = [_to_mac_path(p) for p in (job.get("paths") or [])]
    if not dest_dir:
        return [p for p in paths if os.path.exists(p)]
    os.makedirs(dest_dir, exist_ok=True)
    out = []
    for p in paths:
        if not os.path.exists(p):
            log.warning("nas_downloader: delivered file missing over SMB: %s", p)
            continue
        dst = os.path.join(dest_dir, os.path.basename(p))
        try:
            os.rename(p, dst)
        except OSError:
            try:
                shutil.move(p, dst)
            except OSError:
                log.exception("nas_downloader: couldn't move %s -> %s", p, dst)
                out.append(p)
                continue
        out.append(dst)
    # tidy the emptied staging job dir (best-effort)
    sd = _to_mac_path(job.get("staging_dir") or "")
    try:
        if sd and os.path.isdir(sd) and not any(
                fn for fn in os.listdir(sd) if fn not in ("ready.json", "_rejected")):
            pass  # leave the manifest; NAS-side cleanup owns deletion
    except OSError:
        pass
    return out


def enqueue_and_wait(source: str, *, mode: str = "album", dest_dir: str | None = None,
                     artist=None, album=None, title=None, sample_song=None,
                     spotify_url=None, track_ids=None, kwargs: dict | None = None,
                     timeout_seconds: int = 600) -> dict:
    """Run one acquisition on the NAS daemon; return the adapter-style dict."""
    # LEGAL-USE GATE (authoritative): every source adapter funnels its download through this
    # function, so nothing can be acquired until the user has attested legal use (the setup
    # wizard's Agreement step / the native app's gate). Fail-closed; returns the adapters'
    # normal failure shape so every caller treats it as a failed source attempt.
    try:
        from .db import get_config as _gc
        _attested = (_gc("ownership_attested", "0") or "0") == "1"
    except Exception:
        _attested = False
    if not _attested:
        return {"success": False, "paths": [], "source": source,
                "error": "legal attestation required — downloads blocked until you agree"}
    body = {"source": source, "mode": mode, "artist": artist, "album": album,
            "title": title, "sample_song": sample_song, "spotify_url": spotify_url,
            "track_ids": track_ids, "kwargs": kwargs or {}}
    try:
        job_id = _req("/enqueue", body)["job_id"]
    except Exception as e:
        return {"success": False, "paths": [], "source": source,
                "error": "downloader unreachable: %s" % str(e)[:120]}
    deadline = time.time() + timeout_seconds + _QUEUE_GRACE
    job = None
    while time.time() < deadline:
        try:
            job = _req("/job/" + job_id)
        except Exception:
            time.sleep(_POLL_SECS)
            continue
        if job.get("status") in ("ready", "failed"):
            break
        time.sleep(_POLL_SECS)
    if not job or job.get("status") not in ("ready", "failed"):
        return {"success": False, "paths": [], "source": source, "job_id": job_id,
                "error": "downloader timeout waiting for job %s" % job_id}
    if job["status"] == "failed":
        return {"success": False, "paths": [], "source": source, "job_id": job_id,
                "error": job.get("error") or job.get("adapter_error") or "failed"}
    local = _collect(job, dest_dir)
    return {"success": bool(local), "paths": local, "source": source, "job_id": job_id,
            "bytes_total": sum(os.path.getsize(p) for p in local if os.path.exists(p)),
            "error": None if local else "delivered files not visible over SMB",
            "rejects": job.get("rejects") or []}
