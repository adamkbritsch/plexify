"""plexify-downloader — the NAS-side "dumb" acquisition daemon.

The split (Mac = organization + UI, NAS = autonomous downloading):
this daemon drains a tiny persistent file-queue, downloads each job via ONE
source adapter (soulseek / spotiflac / squid / telegram — the same modules the
full engine uses), verifies the results (fLaC magic + ffmpeg decode, done HERE
near storage where reads are fast), dumps verified files into a per-job
STAGING dir, and writes a ready.json manifest. It never touches plexify.db,
never organizes, never talks to Plex/Spotify — the Mac engine consumes the
staging dirs over SMB and does all of that.

Autonomy: the queue is files on disk (queued/ -> running/ -> ready|failed/),
so enqueued work survives restarts and downloads proceed with the Mac offline.

Runs from the existing plexify image with its OWN DATA_DIR (a fresh small DB
holds only the adapter config keys, seeded from /data/seed_config.json).

HTTP API (:8788, optional bearer via DOWNLOADER_TOKEN):
  GET  /healthz            -> {ok, queued, running, ready, failed}
  POST /enqueue            -> {job_id}   body: {source, mode, artist, album,
                                          title, sample_song, track_ids, kwargs}
  GET  /job/<id>           -> the job JSON (status, staging_dir, paths, error)
  GET  /queue              -> all jobs' JSON, newest first
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("downloader")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s downloader :: %(message)s")

# Identify this process as the NAS downloader so the source adapters run their REAL
# near-storage download implementations instead of delegating back to this daemon
# (their public acquire* names are otherwise the Mac-engine delegation stubs).
os.environ.setdefault("PLEXIFY_DOWNLOADER_DAEMON", "1")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
QUEUE_DIR = os.path.join(DATA_DIR, "queue")
# Job ids are "YYYYMMDD-HHMMSS-<hex>" — restrict lookups to that alphabet so a
# crafted /job/<id> can't traverse out of the queue dir (e.g. /job/../../seed_config).
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9-]+$")
STAGING_ROOT = os.environ.get("STAGING_ROOT", "/downloads_music/staging")
PORT = int(os.environ.get("DOWNLOADER_PORT", "8788"))
TOKEN = os.environ.get("DOWNLOADER_TOKEN", "")
STATES = ("queued", "running", "ready", "failed")
PRUNE_HOURS = float(os.environ.get("PRUNE_HOURS", "24"))
_last_prune = [0.0]


def _seed_config():
    """Seed the daemon's own (fresh) app_config with the adapter keys it needs
    (slskd url/key, squid base, spotiflac + telegram settings) from
    /data/seed_config.json — written once at deploy from the main DB."""
    seed = os.path.join(DATA_DIR, "seed_config.json")
    try:
        with open(seed, encoding="utf-8") as f:
            kv = json.load(f)
    except (OSError, json.JSONDecodeError):
        log.info("no seed_config.json — relying on existing config")
        return
    from app.db import get_config, set_config
    n = 0
    for k, v in kv.items():
        if v and not get_config(k, ""):
            set_config(k, v)
            n += 1
    log.info("seeded %d config keys from seed_config.json", n)


# ---------------------------------------------------------------- job store
def _jpath(state: str, job_id: str) -> str:
    return os.path.join(QUEUE_DIR, state, job_id + ".json")


def _write_job(state: str, job: dict):
    p = _jpath(state, job["id"])
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(job, f, indent=1)
    os.replace(tmp, p)  # atomic


def _move_job(old: str, new: str, job: dict):
    job["status"] = new
    job["updated"] = time.time()
    _write_job(new, job)
    try:
        os.remove(_jpath(old, job["id"]))
    except OSError:
        pass


def _find_job(job_id: str):
    for st in STATES:
        p = _jpath(st, job_id)
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError):
                return None
    return None


def _all_jobs():
    out = []
    for st in STATES:
        d = os.path.join(QUEUE_DIR, st)
        for fn in os.listdir(d) if os.path.isdir(d) else []:
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(d, fn), encoding="utf-8") as f:
                        out.append(json.load(f))
                except (OSError, json.JSONDecodeError):
                    pass
    out.sort(key=lambda j: j.get("created", 0), reverse=True)
    return out


# ---------------------------------------------------------------- verification
def _verify_flac(p: str):
    """fLaC magic + full ffmpeg decode (near storage, fast). Error string or None."""
    try:
        with open(p, "rb") as fh:
            if fh.read(4) != b"fLaC":
                return "not FLAC format (lossy/mislabeled .flac)"
    except OSError as e:
        return "unreadable: %s" % e
    try:
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", p, "-f", "null", "-"],
                           capture_output=True, text=True, timeout=120)
        se = (r.stderr or "").strip()
        if r.returncode != 0 or se:
            return (se or "exit=%d" % r.returncode)[:160]
    except Exception as e:
        return str(e)[:160]
    return None


def _verify_and_stage(paths, staging_dir: str):
    """Verify each file the adapter delivered (wherever it landed) and MOVE the
    good ones into staging_dir (same-volume rename). Bad -> _rejected/."""
    good, rejects = [], []
    for p in paths or []:
        if not p or not os.path.isfile(p):
            continue
        base = os.path.basename(p)
        if not base.lower().endswith(".flac"):
            dst = os.path.join(staging_dir, base)
            try:
                shutil.move(p, dst); good.append(dst)
            except OSError:
                pass
            continue
        err = _verify_flac(p)
        if err is None:
            dst = os.path.join(staging_dir, base)
            try:
                shutil.move(p, dst); good.append(dst)
            except OSError as e:
                rejects.append({"file": base, "error": "stage move failed: %s" % e})
        else:
            rej = os.path.join(staging_dir, "_rejected")
            os.makedirs(rej, exist_ok=True)
            try:
                shutil.move(p, os.path.join(rej, base))
            except OSError:
                pass
            rejects.append({"file": base, "error": err})
    return good, rejects


def _verify_dir(staging_dir: str) -> tuple[list, list]:
    """fLaC magic + full ffmpeg decode for every .flac in the job's staging dir
    (runs on the NAS where the files are local — fast). Non-audio files are left
    alone; bad audio moves to _rejected/. Returns (good_paths, rejects)."""
    good, rejects = [], []
    for dp, dn, fns in os.walk(staging_dir):
        if os.path.basename(dp) == "_rejected":
            continue
        for fn in fns:
            if not fn.lower().endswith(".flac"):
                continue
            p = os.path.join(dp, fn)
            err = None
            try:
                with open(p, "rb") as f:
                    if f.read(4) != b"fLaC":
                        err = "not FLAC format (lossy/mislabeled .flac)"
            except OSError as e:
                err = "unreadable: %s" % e
            if err is None:
                try:
                    r = subprocess.run(["ffmpeg", "-v", "error", "-i", p, "-f", "null", "-"],
                                       capture_output=True, text=True, timeout=120)
                    se = (r.stderr or "").strip()
                    if r.returncode != 0 or se:
                        err = (se or "exit=%d" % r.returncode)[:160]
                except Exception as e:
                    err = str(e)[:160]
            if err is None:
                good.append(p)
            else:
                rej = os.path.join(staging_dir, "_rejected")
                os.makedirs(rej, exist_ok=True)
                try:
                    shutil.move(p, os.path.join(rej, fn))
                except OSError:
                    pass
                rejects.append({"file": fn, "error": err})
    return good, rejects


# ---------------------------------------------------------------- acquisition
def _acquire(job: dict, staging_dir: str) -> dict:
    """Run ONE source adapter into staging_dir. The Mac picker owns source
    fallback/ordering — this daemon just executes the requested source."""
    src = job.get("source", "")
    kw = dict(job.get("kwargs") or {})
    artist, album = job.get("artist"), job.get("album")
    title, sample = job.get("title"), job.get("sample_song")
    if src == "soulseek":
        from app import slskd_picker
        if job.get("mode") == "track":
            return slskd_picker.acquire_track(artist=artist, title=title or sample,
                                              download_dir=staging_dir, **kw)
        return slskd_picker.acquire_album(artist=artist, album=album,
                                          download_dir=staging_dir, **kw)
    if src == "spotiflac":
        from app import spotiflac_adapter
        if not job.get("spotify_url"):
            return {"success": False, "error": "spotiflac requires spotify_url"}
        return spotiflac_adapter.acquire(job["spotify_url"], staging_dir, **kw)
    if src == "squid":
        from app import squid_adapter
        if job.get("mode") == "track":
            return squid_adapter.acquire_track(artist=artist, title=title or sample,
                                               dest_dir=staging_dir, **kw)
        return squid_adapter.acquire(job.get("spotify_url"), artist=artist, album=album,
                                     sample_song=sample, track_ids=job.get("track_ids"),
                                     dest_dir=staging_dir, **kw)
    if src == "telegram":
        from app import telegram_picker
        return telegram_picker.acquire(artist=artist, album=album,
                                       sample_song=sample, dest_dir=staging_dir, **kw)
    return {"success": False, "error": "unknown source %r" % src}


def _run_job(job: dict):
    staging_dir = os.path.join(STAGING_ROOT, job["id"])
    os.makedirs(staging_dir, exist_ok=True)
    job["staging_dir"] = staging_dir
    t0 = time.time()
    try:
        res = _acquire(job, staging_dir)
    except Exception as e:
        log.exception("job %s: adapter raised", job["id"])
        res = {"success": False, "error": "adapter exception: %s" % str(e)[:200]}
    # adapters return a dict (slskd/squid/telegram) or a dataclass (spotiflac)
    if isinstance(res, dict):
        err = res.get("error") or None
        raw_paths = res.get("paths") or []
    else:
        err = getattr(res, "error", None) or None
        raw_paths = list(getattr(res, "paths", []) or [])
    good, rejects = _verify_and_stage(raw_paths, staging_dir)
    job.update({
        "paths": good,
        "rejects": rejects,
        "bytes": sum(os.path.getsize(p) for p in good if os.path.exists(p)),
        "secs": round(time.time() - t0, 1),
        "adapter_error": err,
    })
    if good:
        # verified audio landed -> ready (even on partial adapter errors)
        manifest = os.path.join(staging_dir, "ready.json")
        with open(manifest + ".tmp", "w", encoding="utf-8") as f:
            json.dump(job, f, indent=1)
        os.replace(manifest + ".tmp", manifest)
        _move_job("running", "ready", job)
        log.info("job %s READY: %d files, %d bytes (%ss)", job["id"], len(good), job["bytes"], job["secs"])
    else:
        job["error"] = err or (rejects and "all files failed verification") or "no files acquired"
        _move_job("running", "failed", job)
        try:
            if not os.listdir(staging_dir):
                os.rmdir(staging_dir)
        except OSError:
            pass
        log.info("job %s FAILED: %s", job["id"], job["error"])


def _sweep_collected():
    """Drop ready jobs whose staged files were already COLLECTED by the organizer.

    The Mac moves the audio out of the staging dir over SMB and tidies the emptied
    dir, but the job JSON sat in ready/ until the 24h prune — so the dashboard's
    'Staging: N ready' counted albums that were fully organized hours ago
    (found 2026-07-06). 'ready' should mean staged-and-WAITING: once the staging
    dir is gone (or holds nothing but the manifest/rejects), the job is done."""
    d = os.path.join(QUEUE_DIR, "ready")
    for fn in (list(os.listdir(d)) if os.path.isdir(d) else []):
        if not fn.endswith(".json"):
            continue
        p = os.path.join(d, fn)
        try:
            with open(p, encoding="utf-8") as fh:
                job = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        sd = job.get("staging_dir")
        if not sd:
            continue
        if os.path.isdir(sd):
            try:
                leftover = [x for x in os.listdir(sd)
                            if x not in ("ready.json", "_rejected") and not x.startswith(".")]
            except OSError:
                continue                      # unreadable — leave it for the prune
            if leftover:
                continue                      # still waiting to be organized
            shutil.rmtree(sd, ignore_errors=True)
        try:
            os.remove(p)
            log.info("collected sweep: dropped ready job %s (%s)", job.get("id"), job.get("artist"))
        except OSError:
            pass


def _prune():
    """Drop ready/failed job records + their staging dirs older than PRUNE_HOURS."""
    cut = time.time() - PRUNE_HOURS * 3600
    for st in ("ready", "failed"):
        d = os.path.join(QUEUE_DIR, st)
        for fn in (list(os.listdir(d)) if os.path.isdir(d) else []):
            if not fn.endswith(".json"):
                continue
            p = os.path.join(d, fn)
            try:
                with open(p, encoding="utf-8") as fh:
                    job = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            if job.get("updated", 0) < cut:
                sd = job.get("staging_dir")
                if sd and os.path.isdir(sd):
                    shutil.rmtree(sd, ignore_errors=True)
                try:
                    os.remove(p)
                except OSError:
                    pass


def _worker():
    qdir = os.path.join(QUEUE_DIR, "queued")
    while True:
        try:
            pend = sorted(fn for fn in os.listdir(qdir) if fn.endswith(".json"))
            if not pend:
                if time.time() - _last_prune[0] > 3600:
                    _prune(); _last_prune[0] = time.time()
                time.sleep(2)
                continue
            with open(os.path.join(qdir, pend[0]), encoding="utf-8") as f:
                job = json.load(f)
            _move_job("queued", "running", job)
            _run_job(job)
        except Exception:
            log.exception("worker loop error")
            time.sleep(5)


# ---------------------------------------------------------------- HTTP API
class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        if not TOKEN:
            return True
        return hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + TOKEN)

    def do_GET(self):
        try:
            _sweep_collected()                # self-heal: collected jobs leave 'ready' NOW
        except Exception:
            log.exception("collected sweep failed (continuing)")
        counts = {st: len([f for f in (os.listdir(os.path.join(QUEUE_DIR, st))
                                       if os.path.isdir(os.path.join(QUEUE_DIR, st)) else [])
                           if f.endswith(".json")]) for st in STATES}
        if self.path == "/healthz":
            return self._send(200, {"ok": True, **counts})
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        if self.path == "/queue":
            return self._send(200, {"jobs": _all_jobs(), **counts})
        if self.path.startswith("/job/"):
            jid = self.path[5:].strip("/")
            if not _JOB_ID_RE.match(jid):
                return self._send(400, {"error": "bad job id"})
            job = _find_job(jid)
            return self._send(200, job) if job else self._send(404, {"error": "not found"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        if self.path != "/enqueue":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n).decode() or "{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "bad json"})
        if body.get("source") not in ("soulseek", "spotiflac", "squid", "telegram"):
            return self._send(400, {"error": "source must be one of soulseek|spotiflac|squid|telegram"})
        # Apply the engine's forwarded source config (slskd/squid/telegram creds etc.) to this
        # daemon's OWN config DB — it starts fresh and the setup wizard only writes the engine's
        # DB, so without this every delegated download would see an unconfigured source. Creds
        # are applied to config, never written into the on-disk job file below.
        cfg = body.get("config") or {}
        if cfg:
            try:
                from app.db import set_config
                for k, v in cfg.items():
                    set_config(k, v)
            except Exception:
                log.exception("enqueue: applying forwarded config failed")
        job = {
            "id": time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8],
            "status": "queued", "created": time.time(),
            "source": body["source"], "mode": body.get("mode", "album"),
            "spotify_url": body.get("spotify_url"),
            "artist": body.get("artist"), "album": body.get("album"),
            "title": body.get("title"), "sample_song": body.get("sample_song"),
            "track_ids": body.get("track_ids"), "kwargs": body.get("kwargs") or {},
        }
        _write_job("queued", job)
        log.info("enqueued %s: %s %s / %s", job["id"], job["source"], job["artist"], job["album"] or job["title"])
        return self._send(200, {"job_id": job["id"]})

    def log_message(self, fmt, *args):  # quiet the default request spam
        pass


def main():
    for st in STATES:
        os.makedirs(os.path.join(QUEUE_DIR, st), exist_ok=True)
    os.makedirs(STAGING_ROOT, exist_ok=True)
    from app.db import init_db
    init_db()  # the daemon's OWN fresh DB (config only) — never plexify.db
    _seed_config()
    # Patch SpotiFLAC-main's recurring 'NameError: Optional' regression before any
    # spotiflac job imports it. The engine does this in app.main; the daemon never
    # loads app.main, so it must patch here too.
    try:
        from app.autofill_engine import patch_spotiflac_future_annotations as _patch_sf
        _patch_sf()
    except Exception:
        log.exception("SpotiFLAC startup patch failed (continuing)")
    # crash recovery: anything left 'running' goes back to the queue
    rdir = os.path.join(QUEUE_DIR, "running")
    for fn in list(os.listdir(rdir)):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(rdir, fn), encoding="utf-8") as f:
                    job = json.load(f)
                _move_job("running", "queued", job)
                log.info("recovered zombie running job %s -> queued", job.get("id"))
            except (OSError, json.JSONDecodeError):
                pass
    threading.Thread(target=_worker, daemon=True, name="drain").start()
    log.info("plexify-downloader up: staging=%s queue=%s port=%d", STAGING_ROOT, QUEUE_DIR, PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
