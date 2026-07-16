"""HTTP routes and JSON API endpoints (Flask blueprint)."""

import logging
import os
import threading
from datetime import datetime
from typing import Optional

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from sqlalchemy import desc, select

from . import auth_spotify, jobs, plex_client, spotify_client
from .config import SPOTIFY_REDIRECT_URI
from .db import (
    AppConfig, PlaylistPair, UnmatchedTrack,
    get_config, session_scope, set_config,
)
from .sync_engine import sync_all, sync_pair, rebuild_destinations, compile_playlist, mirror_from_local, trigger_immediate_watcher

bp = Blueprint("main", __name__)

@bp.app_context_processor
def _inject_get_config():
    return dict(get_config=get_config)
log = logging.getLogger(__name__)

UNMATCHED_DISPLAY_LIMIT = 500
RECENT_RUN_LIMIT = 25


# -- First-run setup wizard: state model --
_SETUP_NUMBERED = ["spotify", "plex", "soulseek", "lidarr", "library", "sources", "attest"]
_SETUP_LABELS = {"spotify": "Spotify", "plex": "Plex", "soulseek": "Soulseek",
                 "lidarr": "Lidarr", "library": "Library", "sources": "Sources",
                 "attest": "Agreement"}
_SETUP_REQUIRED = {"spotify", "plex", "attest"}
_SETUP_ALL = ["welcome"] + _SETUP_NUMBERED + ["finish"]


def _setup_skipped() -> set:
    import json as _json
    try:
        return set(_json.loads(get_config("setup_skipped_json", "[]") or "[]"))
    except Exception:
        return set()


def _setup_state() -> dict:
    skipped = _setup_skipped()
    done = {
        "spotify": auth_spotify.is_authed(),
        "plex": plex_client.is_authed(),
        "soulseek": bool(get_config("slskd_url")),
        "lidarr": bool(get_config("lidarr_url")),
        "library": bool(get_config("plex_library_path")),
        "sources": bool(get_config("spotiflac_qobuz_token") or get_config("telegram_enabled") == "1"),
        "attest": (get_config("ownership_attested", "0") or "0") == "1",
    }
    return {k: {"done": done[k], "skipped": k in skipped,
                "required": k in _SETUP_REQUIRED, "label": _SETUP_LABELS[k]}
            for k in _SETUP_NUMBERED}


def _setup_gate_ok() -> bool:
    # Legal-use attestation is a hard gate alongside Spotify + Plex — an un-attested user is
    # routed back into the wizard, and downloading is blocked at the transport regardless.
    return (auth_spotify.is_authed() and plex_client.is_authed()
            and (get_config("ownership_attested", "0") or "0") == "1")


def _setup_complete() -> bool:
    return _setup_gate_ok()


def _setup_next(step: str) -> str:
    try:
        return _SETUP_ALL[min(_SETUP_ALL.index(step) + 1, len(_SETUP_ALL) - 1)]
    except ValueError:
        return "finish"


def _setup_current(state: dict) -> str:
    for k in _SETUP_NUMBERED:
        if not state[k]["done"] and not state[k]["skipped"]:
            return k
    return "finish"


def _setup_pending() -> dict:
    state = _setup_state()
    pend = [k for k in _SETUP_NUMBERED
            if k not in _SETUP_REQUIRED and not state[k]["done"] and not state[k]["skipped"]]
    return {"count": len(pend), "first": pend[0] if pend else None}


@bp.route("/healthz")
def healthz():
    return "ok", 200


# ====================================================================
# Dashboard
# ====================================================================
@bp.route("/")
def dashboard():
    if not _setup_gate_ok():
        return redirect(url_for("main.setup"))
    return render_template("dashboard.html", setup_pending=_setup_pending())

# ====================================================================
# Setup — Spotify
# ====================================================================
# The Plexify music library folder is ALWAYS named this and is never
# user-renameable (a separate, non-Plexify folder is the answer for other names).
PLEXIFY_MUSIC_DIRNAME = "plexify-music"


@bp.route("/setup")
def setup():
    state = _setup_state()
    step = request.args.get("step")
    if step not in _SETUP_ALL:
        step = "welcome" if not any(state[k]["done"] for k in _SETUP_NUMBERED) else _setup_current(state)
    ctx = {}
    if step == "spotify":
        ctx = dict(client_id=get_config("spotify_client_id", ""),
                   redirect_uri=SPOTIFY_REDIRECT_URI, spotify_authed=auth_spotify.is_authed())
    elif step == "plex":
        ctx = dict(plex_url=get_config("plex_url", ""), plex_token=get_config("plex_token", ""),
                   plex_section_key=get_config("plex_music_section_key", ""),
                   sections=plex_client.list_music_sections(), health=plex_client.check_health())
    elif step == "soulseek":
        ctx = dict(slskd_url=get_config("slskd_url", "") or "http://slskd:5030",
                   slskd_api_key=get_config("slskd_api_key", ""))
    elif step == "lidarr":
        ctx = dict(lidarr_url=get_config("lidarr_url", "") or "http://plexify-lidarr:8686",
                   lidarr_api_key=get_config("lidarr_api_key", ""))
    elif step == "library":
        ctx = dict(library_path=get_config("plex_library_path", "") or "/media/vol3/Music")
    elif step == "sources":
        ctx = dict(qobuz_token=get_config("spotiflac_qobuz_token", ""),
                   telegram_enabled=(get_config("telegram_enabled") == "1"))
    idx = (_SETUP_NUMBERED.index(step) + 1) if step in _SETUP_NUMBERED else 0
    return render_template("setup_wizard.html", step=step, state=state,
                           numbered=_SETUP_NUMBERED, labels=_SETUP_LABELS,
                           step_index=idx, total=len(_SETUP_NUMBERED),
                           next_step=_setup_next(step), **ctx)


@bp.route("/setup/spotify", methods=["POST"])
def setup_spotify():
    cid = request.form.get("client_id", "").strip()
    sec = request.form.get("client_secret", "").strip()
    if not (cid and sec):
        flash("Both Spotify client ID and secret are required.", "error")
    else:
        set_config("spotify_client_id", cid)
        set_config("spotify_client_secret", sec)
        flash("Saved -- now click Authorize to connect your account.", "success")
    return redirect(url_for("main.setup", step="spotify"))


@bp.route("/setup/soulseek", methods=["POST"])
def setup_soulseek():
    url = request.form.get("slskd_url", "").strip().rstrip("/")
    key = request.form.get("slskd_api_key", "").strip()
    if url:
        set_config("slskd_url", url)
    if key:
        set_config("slskd_api_key", key)
    flash("Soulseek (slskd) saved.", "success")
    return redirect(url_for("main.setup", step="lidarr"))


@bp.route("/setup/lidarr", methods=["POST"])
def setup_lidarr():
    url = request.form.get("lidarr_url", "").strip().rstrip("/")
    key = request.form.get("lidarr_api_key", "").strip()
    if url:
        set_config("lidarr_url", url)
    if key:
        set_config("lidarr_api_key", key)
    flash("Lidarr saved.", "success")
    return redirect(url_for("main.setup", step="library"))


@bp.route("/setup/library", methods=["POST"])
def setup_library():
    path = request.form.get("library_path", "").strip().rstrip("/")
    if path:
        set_config("plex_library_path", path)
    flash("Library path saved.", "success")
    return redirect(url_for("main.setup", step="sources"))


@bp.route("/setup/sources", methods=["POST"])
def setup_sources():
    tok = request.form.get("qobuz_token", "").strip()
    if tok:
        set_config("spotiflac_qobuz_token", tok)
    set_config("telegram_enabled", "1" if request.form.get("telegram_enabled") == "1" else "0")
    flash("Sources saved.", "success")
    return redirect(url_for("main.setup", step="attest"))


@bp.route("/setup/attest", methods=["POST"])
def setup_attest():
    """Legal-use agreement — required, no skip. Unlocks downloading (gate in nas_downloader)."""
    if request.form.get("agree") != "1":
        flash("Please confirm the agreement to continue.", "error")
        return redirect(url_for("main.setup", step="attest"))
    from datetime import datetime as _dt
    set_config("ownership_attested", "1")
    set_config("ownership_attested_at", _dt.utcnow().isoformat() + "Z")
    flash("Agreement recorded — you're all set.", "success")
    return redirect(url_for("main.setup", step="finish"))


@bp.route("/setup/skip/<step>", methods=["POST"])
def setup_skip(step):
    import json as _json
    if step in _SETUP_NUMBERED and step not in _SETUP_REQUIRED:
        sk = _setup_skipped()
        sk.add(step)
        set_config("setup_skipped_json", _json.dumps(sorted(sk)))
    return redirect(url_for("main.setup", step=_setup_next(step)))


@bp.route("/api/setup/detect-plex")
def api_detect_plex():
    import urllib.request as _u, json as _json, socket as _s
    from urllib.parse import urlparse as _up
    hosts = []
    try:
        hosts.append(_s.gethostbyname("host.docker.internal"))
    except Exception:
        pass
    try:
        with open("/proc/net/route") as _f:
            for _ln in _f.readlines()[1:]:
                _p = _ln.split()
                if len(_p) > 2 and _p[1] == "00000000":
                    hosts.append(".".join(str(int(_p[2][i:i + 2], 16)) for i in (6, 4, 2, 0)))
                    break
    except Exception:
        pass
    cur = get_config("plex_url", "")
    if cur:
        h = _up(cur).hostname
        if h:
            hosts.append(h)
    found, seen, seen_m = [], set(), set()
    for h in hosts:
        if not h or h in seen:
            continue
        seen.add(h)
        url = "http://%s:32400" % h
        try:
            req = _u.Request(url + "/identity", headers={"Accept": "application/json"})
            with _u.urlopen(req, timeout=2) as r:
                mc = _json.loads(r.read().decode()).get("MediaContainer", {})
                m = mc.get("machineIdentifier", "")
                if m and m in seen_m:
                    continue
                seen_m.add(m)
                found.append({"url": url, "machine": m, "version": mc.get("version", "")})
        except Exception:
            pass
    return jsonify({"servers": found})


@bp.route("/auth/spotify/login")
def spotify_login():
    url = auth_spotify.get_authorize_url()
    if not url:
        flash("Spotify credentials not configured.", "error")
        return redirect(url_for("main.setup"))
    return redirect(url)


@bp.route("/auth/spotify/callback")
def spotify_callback():
    code = request.args.get("code")
    error = request.args.get("error")
    if error or not code:
        flash(f"Spotify auth failed: {error or 'no code'}", "error")
        return redirect(url_for("main.dashboard"))
    if auth_spotify.exchange_code(code):
        flash("Spotify connected — auto-discovering your playlists now.", "success")
        # Fire watcher immediately so the user sees discovery happen instead of
        # waiting up to 5 minutes for the next scheduled tick.
        trigger_immediate_watcher()
    else:
        flash("Spotify auth exchange failed. Check your client ID/secret and redirect URI.", "error")
    return redirect(url_for("main.dashboard"))


@bp.route("/auth/spotify/logout", methods=["POST"])
def spotify_logout():
    auth_spotify.revoke()
    flash("Spotify disconnected.", "success")
    return redirect(url_for("main.dashboard"))


# ====================================================================
# Setup — Plex (destination)
# ====================================================================
def _lidarr_api(path, method="GET", payload=None, timeout=10):
    """Minimal authenticated Lidarr API call -> (status_code, parsed_json_or_text)."""
    import json as _j, urllib.request as _u, urllib.error as _ue
    base = (get_config("lidarr_url", "") or "http://plexify-lidarr:8686").rstrip("/")
    req = _u.Request(
        base + path, method=method,
        headers={"X-Api-Key": get_config("lidarr_api_key", ""),
                 "Content-Type": "application/json"},
        data=(_j.dumps(payload).encode() if payload is not None else None),
    )
    try:
        r = _u.urlopen(req, timeout=timeout)
        body = r.read().decode("utf-8", "replace")
        try:
            return r.status, _j.loads(body)
        except Exception:
            return r.status, body
    except _ue.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]
    except Exception as e:
        return 0, str(e)[:300]


def _read_lidarr_apikey():
    """Auto-read plexify-lidarr's generated ApiKey from its config.xml, mounted
    read-only at /lidarr-config (see docker-compose.yml)."""
    import re as _re
    try:
        with open("/lidarr-config/config.xml", encoding="utf-8") as _f:
            _m = _re.search(r"<ApiKey>([a-f0-9]+)</ApiKey>", _f.read())
            return _m.group(1) if _m else ""
    except Exception:
        return ""


@bp.route("/api/setup/lidarr-autowire", methods=["POST"])
def api_setup_lidarr_autowire():
    """Auto-connect the bundled plexify-lidarr: persist URL, auto-read its API
    key, verify reachability, ensure the /music ("plexify-music") root folder."""
    url = (get_config("lidarr_url", "") or "http://plexify-lidarr:8686").rstrip("/")
    set_config("lidarr_url", url)
    key = get_config("lidarr_api_key", "") or _read_lidarr_apikey()
    if not key:
        return jsonify(ok=False, error="Couldn't read plexify-lidarr's API key yet (is the container finished starting?)."), 200
    set_config("lidarr_api_key", key)
    st, status = _lidarr_api("/api/v1/system/status")
    if st != 200:
        return jsonify(ok=False, error="Lidarr not reachable (HTTP %s)." % st), 200
    version = status.get("version", "?") if isinstance(status, dict) else "?"
    root = "/music"
    _st, roots = _lidarr_api("/api/v1/rootfolder")
    have = isinstance(roots, list) and any(
        isinstance(x, dict) and (x.get("path", "") or "").rstrip("/") == root for x in roots)
    root_status = "already present"
    if not have:
        _, _qp = _lidarr_api("/api/v1/qualityprofile")
        _, _mp = _lidarr_api("/api/v1/metadataprofile")
        _qid = _qp[0]["id"] if isinstance(_qp, list) and _qp else None
        _mid = _mp[0]["id"] if isinstance(_mp, list) and _mp else None
        ast, _rb = _lidarr_api("/api/v1/rootfolder", method="POST", payload={
            "name": PLEXIFY_MUSIC_DIRNAME, "path": root,
            "defaultQualityProfileId": _qid, "defaultMetadataProfileId": _mid,
            "defaultMonitorOption": "all", "defaultNewItemMonitorOption": "all", "defaultTags": [],
        })
        root_status = "created" if ast in (200, 201) else ("could not auto-create (HTTP %s) -- add /music in Lidarr UI" % ast)
    return jsonify(ok=True, version=version, root_folder=root, root_status=root_status)


@bp.route("/api/setup/test-slskd", methods=["POST"])
def api_setup_test_slskd():
    """Test the slskd connection (GET /api/v0/session). Uses posted url/key if
    provided, else saved config."""
    import urllib.request as _u, urllib.error as _ue
    body = request.get_json(silent=True) or {}
    base = (body.get("slskd_url") or get_config("slskd_url", "") or "http://slskd:5030").rstrip("/")
    key = body.get("slskd_api_key") or get_config("slskd_api_key", "")
    req = _u.Request(base + "/api/v0/session", headers={"X-API-Key": key})
    try:
        r = _u.urlopen(req, timeout=8)
        return jsonify(ok=True, status=r.status)
    except _ue.HTTPError as e:
        return jsonify(ok=(e.code == 200), error="HTTP %s" % e.code), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 200


@bp.route("/setup/plex", methods=["GET", "POST"])
def setup_plex():
    if request.method == "POST":
        url = request.form.get("plex_url", "").strip().rstrip("/")
        token = request.form.get("plex_token", "").strip()
        section_key = request.form.get("plex_music_section_key", "").strip()
        if not (url and token):
            flash("Plex URL and token are required.", "error")
            return redirect(url_for("main.setup", step="plex"))
        set_config("plex_url", url)
        set_config("plex_token", token)
        if section_key:
            set_config("plex_music_section_key", section_key)
        plex_client.invalidate_health_cache()
        health = plex_client.check_health(use_cache=False)
        if not health["connected"]:
            flash(f"Saved, but couldn't connect to Plex: {health.get('error')}", "error")
        elif not health["music_section"]:
            flash("Saved, but couldn't find a Music section. Pick one below.", "warning")
        else:
            flash(f"Plex connected — Music: {health['music_section']} ({health['track_count']} tracks).", "success")
        return redirect(url_for("main.setup", step="plex"))
    return redirect(url_for("main.setup", step="plex"))


@bp.route("/setup/plex/disconnect", methods=["POST"])
def disconnect_plex():
    with session_scope() as s:
        s.query(AppConfig).filter(
            AppConfig.key.in_(["plex_url", "plex_token", "plex_music_section_key"])
        ).delete(synchronize_session=False)
    plex_client.invalidate_health_cache()
    flash("Plex disconnected.", "success")
    return redirect(url_for("main.dashboard"))


# ====================================================================
# Playlists (Spotify-source-only)
# ====================================================================
@bp.route("/playlists")
def playlists():
    if not auth_spotify.is_authed():
        flash("Connect Spotify first — it's the source of truth for sync.", "error")
        return redirect(url_for("main.dashboard"))
    plex_ok = plex_client.is_authed()

    sp_error: Optional[str] = None
    try:
        sp = spotify_client.list_my_playlists()
    except Exception as e:
        log.warning("/playlists: list_my_playlists failed: %s", e)
        sp = []
        sp_error = (f"Spotify couldn't return your playlists right now — likely rate-limited "
                    f"({type(e).__name__}). Wait a few minutes and refresh.")
    plex_pls = []
    try:
        if plex_ok:
            plex_pls = plex_client.list_playlists()
    except Exception as e:
        log.warning("/playlists: plex list failed: %s", e)
    with session_scope() as s:
        existing = list(s.scalars(select(PlaylistPair)).all())
        mapped_sp = {p.spotify_playlist_id for p in existing if p.spotify_playlist_id}
        # Index pairs by Spotify ID so the template can show current mirror state
        by_sp = {p.spotify_playlist_id: p for p in existing if p.spotify_playlist_id}

    return render_template(
        "playlists.html",
        spotify_playlists=sp,
        spotify_error=sp_error,
        plex_playlists=plex_pls,
        plex_ok=plex_ok,
        pairs=existing,
        mapped_sp=mapped_sp,
        pair_by_spotify=by_sp,
    )


@bp.route("/playlists/pair", methods=["POST"])
def create_pair():
    """Mirror a Spotify playlist to selected destinations. Spotify source is required."""
    sp_id = request.form.get("spotify_id", "").strip() or None
    auto_plex = request.form.get("auto_fill_plex") == "1"
    name = request.form.get("name", "").strip()

    if not sp_id or not auth_spotify.is_authed():
        flash("Pick a Spotify playlist (Spotify is the only allowed source).", "error")
        return redirect(url_for("main.playlists"))

    try:
        sp_list = {p.id: p for p in spotify_client.list_my_playlists()}
    except Exception:
        log.exception("create_pair: list_my_playlists failed")
        flash("Couldn't reach Spotify just now (it may be rate-limiting). Try again in a moment.", "error")
        return redirect(url_for("main.playlists"))
    if sp_id not in sp_list:
        flash("Spotify playlist not found.", "error")
        return redirect(url_for("main.playlists"))

    display = name or sp_list[sp_id].name
    plex_ok = plex_client.is_authed()

    pending_create_name: Optional[str] = None
    new_pair_id: Optional[int] = None
    with session_scope() as s:
        pair = PlaylistPair(
            name=display, spotify_playlist_id=sp_id, enabled=True,
        )
        if plex_ok and auto_plex:
            pair.plex_enabled = True
            pending_create_name = display
        s.add(pair)
        s.flush()
        new_pair_id = pair.id

    if new_pair_id is not None and pending_create_name:
        set_config(f"plex_pending_create_{new_pair_id}", pending_create_name)

    if new_pair_id is not None:
        # Compile-then-mirror, as a tracked job. Compile is incremental + resumable;
        # if it crashes mid-way, the watcher picks it back up on the next tick.
        job_id = jobs.create("compile_and_mirror", title=f"Build: {display}",
                             pair_id=new_pair_id, total=0)

        def runner(pid=new_pair_id, jid=job_id, dn=display):
            jobs.start(jid, step=f"Compiling local backup of {dn}")
            try:
                r = compile_playlist(pid, job_id=jid)
                if r["status"] == "ok":
                    mirror_from_local(pid, job_id=jid)
                    jobs.finish(jid, status="ok",
                                message=f"{dn} built ({r.get('final_offset', 0)} tracks)")
                elif r["status"] == "paused":
                    jobs.finish(jid, status="partial",
                                message=f"{dn} compile paused at {r.get('final_offset', 0)}; resumes next tick")
                else:
                    jobs.fail(jid, r.get("error") or "compile failed")
            except Exception as e:
                log.exception("compile+mirror runner failed: %s", e)
                jobs.fail(jid, str(e))

        threading.Thread(target=runner, daemon=True).start()
        if request.headers.get("Accept", "").startswith("application/json") or request.args.get("json"):
            return jsonify({"job_id": job_id, "pair_id": new_pair_id, "name": display})
        return redirect(url_for("main.job_view", job_id=job_id))
    return redirect(url_for("main.playlists"))


@bp.route("/playlists/mirror-all", methods=["POST"])
def mirror_all():
    """Create a mirror pair for every Spotify playlist the user owns."""
    if not auth_spotify.is_authed():
        if request.args.get("json"):
            return jsonify({"error": "spotify not authenticated"}), 400
        flash("Connect Spotify first.", "error")
        return redirect(url_for("main.playlists"))

    # C3: unchecked checkbox isn't in the form; missing must mean OFF, not ON.
    auto_plex = request.form.get("auto_fill_plex") == "1"

    job_id = jobs.create("mirror_all", title="Mirror all Spotify playlists", total=0)

    def runner():
        try:
            _mirror_all_run(job_id=job_id, auto_plex=auto_plex)
        except Exception as e:
            log.exception("mirror_all failed: %s", e)
            jobs.fail(job_id, str(e))

    threading.Thread(target=runner, daemon=True).start()
    if request.headers.get("Accept", "").startswith("application/json") or request.args.get("json"):
        return jsonify({"job_id": job_id})
    return redirect(url_for("main.job_view", job_id=job_id))


def _mirror_all_run(*, job_id: int, auto_plex: bool) -> None:
    jobs.start(job_id, step="Listing Spotify playlists…")
    plex_ok = plex_client.is_authed()

    src_playlists = [{"id": p.id, "name": p.name} for p in spotify_client.list_my_playlists()]
    jobs.progress(job_id, current=0, total=len(src_playlists))
    jobs.emit(job_id, f"Found {len(src_playlists)} Spotify playlists you own")
    if not plex_ok:
        jobs.emit(job_id, "Plex not connected — skipping Plex destinations", level="warning")

    with session_scope() as s:
        existing = list(s.scalars(select(PlaylistPair)).all())
        already_mapped = {p.spotify_playlist_id for p in existing if p.spotify_playlist_id}

    created = 0
    skipped = 0
    for i, src in enumerate(src_playlists, 1):
        jobs.progress(job_id, current=i, step=f"{i}/{len(src_playlists)}: {src['name']}")

        if src["id"] in already_mapped:
            jobs.emit(job_id, f"skip (already mirrored): {src['name']}")
            skipped += 1
            continue

        plex_enabled_flag = plex_ok and auto_plex

        with session_scope() as s:
            pair = PlaylistPair(
                name=src["name"], spotify_playlist_id=src["id"],
                plex_enabled=plex_enabled_flag,
                enabled=True,
            )
            s.add(pair)
            s.flush()
            new_pair_id = pair.id

        if plex_enabled_flag:
            set_config(f"plex_pending_create_{new_pair_id}", src["name"])

        jobs.emit(job_id, f"  created pair #{new_pair_id} for '{src['name']}'")
        created += 1
        # H8: bound sync_pair to PAIR_SYNC_TIMEOUT_S so one slow Spotify/Tidal
        # playlist can't hang the whole mirror_all batch. Spawned in a child
        # thread; if it overruns the budget we log + move on (the watcher will
        # pick it up later).
        PAIR_SYNC_TIMEOUT_S = 600  # 10 min per pair max
        worker = threading.Thread(
            target=sync_pair, kwargs={"pair_id": new_pair_id, "job_id": job_id},
            daemon=True,
        )
        worker.start()
        worker.join(timeout=PAIR_SYNC_TIMEOUT_S)
        if worker.is_alive():
            jobs.emit(job_id, f"  ⚠ pair {new_pair_id} sync exceeded {PAIR_SYNC_TIMEOUT_S}s; deferring to watcher",
                      level="warning")

    jobs.finish(job_id, status="ok",
                message=f"created {created} pairs, skipped {skipped} already-mirrored",
                result={"created": created, "skipped": skipped})


@bp.route("/playlists/pair/<int:pair_id>/toggle", methods=["POST"])
def toggle_pair(pair_id: int):
    with session_scope() as s:
        pair = s.get(PlaylistPair, pair_id)
        if pair:
            pair.enabled = not pair.enabled
            flash(f"{'Enabled' if pair.enabled else 'Paused'}: {pair.name}", "success")
    return redirect(url_for("main.dashboard"))


@bp.route("/playlists/pair/<int:pair_id>/destination/<service>", methods=["POST"])
def toggle_destination(pair_id: int, service: str):
    """Enable / disable a destination on an existing pair without re-syncing Spotify."""
    if service != "plex":
        abort(400)  # Tidal removed
    action = request.form.get("action", "enable")
    enable = (action == "enable")
    new_pair_id_for_sync = None
    with session_scope() as s:
        pair = s.get(PlaylistPair, pair_id)
        if not pair:
            abort(404)
        name = pair.name
        if service == "plex":
            if enable:
                pair.plex_enabled = True
                if not pair.plex_playlist_key:
                    set_config(f"plex_pending_create_{pair.id}", name)
                flash(f"Enabled Plex mirror on {name}.", "success")
            else:
                pair.plex_enabled = False
                flash(f"Disabled Plex mirror on {name}.", "success")
        new_pair_id_for_sync = pair.id
    # Kick off a mirror_from_local job so the newly-enabled destination gets caught up
    if enable and new_pair_id_for_sync:
        from .sync_engine import mirror_from_local
        job_id = jobs.create("enable_destination",
                             title=f"Enable {service.title()}: {name}",
                             pair_id=new_pair_id_for_sync, total=1)

        def runner(pid=new_pair_id_for_sync, jid=job_id, dn=name):
            jobs.start(jid, step=f"Mirroring {dn} to newly-enabled destination")
            try:
                mirror_from_local(pid, job_id=jid)
                jobs.finish(jid, status="ok", message=f"{dn} mirrored to {service}")
            except Exception as e:
                log.exception("enable-destination mirror failed: %s", e)
                jobs.fail(jid, str(e))

        threading.Thread(target=runner, daemon=True).start()
    return redirect(url_for("main.dashboard"))


@bp.route("/playlists/pair/<int:pair_id>/delete", methods=["POST"])
def delete_pair(pair_id: int):
    from .db import LocalTrack
    from .sync_engine import forget_pair_lock   # Q14
    with session_scope() as s:
        pair = s.get(PlaylistPair, pair_id)
        if pair:
            name = pair.name
            # Delete local backup tracks (cascade not declared on LocalTrack relationship)
            s.query(LocalTrack).filter(LocalTrack.pair_id == pair_id).delete(synchronize_session=False)
            # H7: cascade UnmatchedTrack — relationship has no cascade declared
            s.query(UnmatchedTrack).filter(UnmatchedTrack.pair_id == pair_id).delete(synchronize_session=False)
            s.delete(pair)
            flash(f"Removed mirror: {name} (destination playlists themselves are untouched)", "success")
    # Q14: prune in-memory pair lock so dict doesn't grow unbounded
    try:
        forget_pair_lock(pair_id)
    except Exception:
        pass
    return redirect(url_for("main.dashboard"))


# ====================================================================
# Sync jobs
# ====================================================================
def _spawn_pair_sync(pair_id: int, pair_name: str, *, include_deletions: bool = False) -> int:
    """Manual per-pair sync. Compiles if not yet complete, then mirrors."""
    kind = "sync_pair_full" if include_deletions else "sync_pair"
    title = f"{'Reconcile' if include_deletions else 'Sync'}: {pair_name}"
    job_id = jobs.create(kind, title=title, pair_id=pair_id, total=0)

    def runner():
        jobs.start(job_id, step=f"Starting {title}")
        try:
            with session_scope() as s:
                pair = s.get(PlaylistPair, pair_id)
                status = pair.compile_status if pair else "pending"
            if status != "complete":
                r = compile_playlist(pair_id, job_id=job_id)
                if r["status"] not in ("ok", "skipped"):
                    jobs.finish(job_id, status="partial",
                                message=f"compile {r['status']}: {r.get('error') or 'paused'}")
                    return
            mirror_from_local(pair_id, include_deletions=include_deletions, job_id=job_id)
            jobs.finish(job_id, status="ok", message=f"{title} complete")
        except Exception as e:
            log.exception("manual sync_pair failed: %s", e)
            jobs.fail(job_id, str(e))

    threading.Thread(target=runner, daemon=True).start()
    return job_id


def _spawn_sync_all(*, include_deletions: bool = False) -> int:
    title = "Reconcile all (incl. deletions)" if include_deletions else "Sync all (additions)"
    kind = "sync_all_full" if include_deletions else "sync_all"
    job_id = jobs.create(kind, title=title)
    threading.Thread(
        target=sync_all,
        kwargs={"include_deletions": include_deletions, "job_id": job_id},
        daemon=True,
    ).start()
    return job_id


@bp.route("/playlists/pair/<int:pair_id>/sync", methods=["POST"])
def manual_sync(pair_id: int):
    with session_scope() as s:
        pair = s.get(PlaylistPair, pair_id)
        if not pair:
            abort(404)
        name = pair.name
    include_del = request.form.get("include_deletions") == "1"
    job_id = _spawn_pair_sync(pair_id, name, include_deletions=include_del)
    if request.headers.get("Accept", "").startswith("application/json") or request.args.get("json"):
        return jsonify({"job_id": job_id})
    return redirect(url_for("main.job_view", job_id=job_id))


@bp.route("/playlists/pair/<int:pair_id>/rebuild", methods=["POST"])
def rebuild_pair(pair_id: int):
    """Destructively clear & re-add destination playlists in Spotify's source order.

    By default uses the LOCAL CACHE (no Spotify API call). Pass `use_live=1` to
    force a live Spotify fetch.
    """
    with session_scope() as s:
        pair = s.get(PlaylistPair, pair_id)
        if not pair:
            abort(404)
        name = pair.name
    do_plex = request.form.get("rebuild_plex", "1") == "1"
    use_cache = request.form.get("use_live", "0") != "1"
    job_id = jobs.create("rebuild", title=f"Rebuild: {name}", pair_id=pair_id, total=1)

    def runner():
        jobs.start(job_id, step=f"Rebuilding {name} in Spotify source order")
        try:
            result = rebuild_destinations(pair_id, do_plex=do_plex,
                                          use_cache=use_cache, job_id=job_id)
            status = result.get("status") or "ok"
            if status == "skipped":
                jobs.finish(job_id, status="error",
                            message=f"Skipped: {result.get('error') or 'another sync is running'}")
            elif status == "error":
                jobs.fail(job_id, result.get("error") or "rebuild failed")
            else:
                jobs.progress(job_id, current=1)
                jobs.finish(job_id, status="ok",
                            message=(f"Rebuilt {name} — "
                                     f"+{result.get('rebuilt_plex', 0)} Plex"))
        except Exception as e:
            log.exception("rebuild failed: %s", e)
            jobs.fail(job_id, str(e))
    threading.Thread(target=runner, daemon=True).start()
    if request.headers.get("Accept", "").startswith("application/json") or request.args.get("json"):
        return jsonify({"job_id": job_id})
    return redirect(url_for("main.job_view", job_id=job_id))


PLAYLIST_SORT_MODES = {"source", "recent", "title", "artist", "album"}
PLAYLIST_SORT_LABELS = {
    "source": "Date added", "recent": "Recently added",
    "title": "Title", "artist": "Artist", "album": "Album",
}


@bp.route("/playlists/pair/<int:pair_id>/sort", methods=["POST"])
def set_sort_mode(pair_id: int):
    """Set one playlist's Plex sort order (Spotify-style dropdown) and recompile its Plex
    playlist immediately so the new order is visible right away."""
    mode = (request.form.get("mode") or request.args.get("mode") or "source").strip().lower()
    if mode not in PLAYLIST_SORT_MODES:
        mode = "source"
    with session_scope() as s:
        pair = s.get(PlaylistPair, pair_id)
        if not pair:
            abort(404)
        name = pair.name
        pair.sort_mode = mode
        pair.reverse_order = (mode == "recent")  # keep legacy flag consistent
    label = PLAYLIST_SORT_LABELS.get(mode, mode)
    job_id = jobs.create("rebuild", title=f"Sort: {name}", pair_id=pair_id, total=1)

    def runner():
        jobs.start(job_id, step=f"Reordering {name} by {label}")
        try:
            result = rebuild_destinations(pair_id, do_plex=True,
                                          use_cache=True, job_id=job_id, wait=True)
            if (result.get("status") or "ok") == "error":
                jobs.fail(job_id, result.get("error") or "sort failed")
            else:
                jobs.progress(job_id, current=1)
                jobs.finish(job_id, status="ok",
                            message=f"{name}: sorted by {label} (+{result.get('rebuilt_plex', 0)} Plex)")
        except Exception as e:
            log.exception("sort rebuild failed: %s", e)
            jobs.fail(job_id, str(e))
    threading.Thread(target=runner, daemon=True).start()
    if request.headers.get("X-Requested-With") == "fetch" or request.args.get("json"):
        return jsonify({"job_id": job_id, "mode": mode, "label": label})
    return redirect(url_for("main.job_view", job_id=job_id))


@bp.route("/playlists/recompile-all", methods=["POST"])
def recompile_all_playlists():
    """Reorder EVERY Plex playlist to match its Spotify source order (decoupled, from the
    local cache). Intensive — also runs automatically nightly at 4am."""
    from .sync_engine import recompile_all_plex_playlists
    job_id = jobs.create("recompile_all", title="Recompile all Plex playlist orders", total=1)

    def runner():
        jobs.start(job_id, step="Reordering all Plex playlists to match Spotify")
        try:
            res = recompile_all_plex_playlists(job_id=job_id, wait=True)
            jobs.progress(job_id, current=1)
            jobs.finish(job_id, status="ok",
                        message=(f"Recompiled {res.get('ok', 0)}/{res.get('playlists', 0)} playlists, "
                                 f"{res.get('plex_tracks', 0)} tracks reordered ({res.get('errors', 0)} errors)."))
        except Exception as e:
            log.exception("recompile_all failed")
            jobs.fail(job_id, str(e))
    threading.Thread(target=runner, daemon=True).start()
    if request.headers.get("Accept", "").startswith("application/json") or request.args.get("json"):
        return jsonify({"job_id": job_id})
    return redirect(url_for("main.job_view", job_id=job_id))


@bp.route("/sync/now", methods=["POST"])
def sync_now():
    include_del = request.form.get("include_deletions") == "1"
    job_id = _spawn_sync_all(include_deletions=include_del)
    if request.headers.get("Accept", "").startswith("application/json") or request.args.get("json"):
        return jsonify({"job_id": job_id})
    return redirect(url_for("main.job_view", job_id=job_id))


@bp.route("/api/sync/all", methods=["POST"])
def api_sync_all():
    include_del = request.form.get("include_deletions") == "1" or request.args.get("include_deletions") == "1"
    return jsonify({"job_id": _spawn_sync_all(include_deletions=include_del)})


@bp.route("/api/sync/pair/<int:pair_id>", methods=["POST"])
def api_sync_pair(pair_id: int):
    with session_scope() as s:
        pair = s.get(PlaylistPair, pair_id)
        if not pair:
            return jsonify({"error": "pair not found"}), 404
        name = pair.name
    include_del = request.form.get("include_deletions") == "1"
    return jsonify({"job_id": _spawn_pair_sync(pair_id, name, include_deletions=include_del)})


@bp.route("/api/jobs/<int:job_id>")
def api_job(job_id: int):
    # Q27: tolerate junk in query string
    try:
        after = int(request.args.get("after", "0"))
    except (ValueError, TypeError):
        after = 0
    j = jobs.get(job_id, last_event_id=after)
    if not j:
        return jsonify({"error": "not found"}), 404
    return jsonify(j)


@bp.route("/api/jobs")
def api_active_jobs():
    return jsonify({"active": jobs.active()})


@bp.route("/jobs/<int:job_id>")
def job_view(job_id: int):
    j = jobs.get(job_id)
    if not j:
        abort(404)
    return render_template("job.html", job=j)


@bp.route("/jobs")
def jobs_view():
    return render_template("jobs.html", recent_jobs=jobs.recent(50))


# ====================================================================
# Unmatched
# ====================================================================
@bp.route("/audiobooks")
def audiobooks_view():
    from flask import render_template
    return render_template("audiobooks.html")


@bp.route("/unmatched")
def unmatched_view():
    with session_scope() as s:
        total = s.query(UnmatchedTrack).count()
        rows = list(
            s.scalars(
                select(UnmatchedTrack)
                .order_by(desc(UnmatchedTrack.last_seen_at))
                .limit(UNMATCHED_DISPLAY_LIMIT)
            ).all()
        )
    return render_template(
        "unmatched.html", rows=rows, total=total,
        shown=len(rows), limit=UNMATCHED_DISPLAY_LIMIT,
    )


@bp.route("/unmatched/clear", methods=["POST"])
def clear_unmatched():
    with session_scope() as s:
        s.query(UnmatchedTrack).delete()
    flash("Cleared unmatched log.", "success")
    return redirect(url_for("main.unmatched_view"))


@bp.app_template_filter("dt")
def fmt_dt(value: Optional[datetime]) -> str:
    if not value:
        return "—"
    return value.strftime("%Y-%m-%d %H:%M:%S UTC")


@bp.app_template_filter("ago")
def fmt_ago(value: Optional[datetime]) -> str:
    """Human-friendly relative time: '3m ago', '2h ago', '5d ago'."""
    if not value:
        return "never"
    delta = datetime.utcnow() - value
    s = int(delta.total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


# ====================================================================
# Library Autofill — Spotify → Lidarr (→ slskd) → Plex
# ====================================================================
@bp.route('/library-autofill', methods=['GET'])
def library_autofill():
    # Page merged into Settings (2026-05-31) — redirect old links + form POSTs there.
    return redirect(url_for("main.settings_view"))




@bp.route('/library-autofill/save-toggles', methods=['POST'])
def library_autofill_save_toggles():
    import json as _json
    enabled = request.form.get('autofill_enabled') == 'on'
    set_config('autofill_enabled', '1' if enabled else '0')

    sources: list[str] = []
    if request.form.get('source_liked') == 'on':
        sources.append('liked')
    if request.form.get('source_followed_artists') == 'on':
        sources.append('followed_artists')
    for pid in request.form.getlist('source_playlist'):
        if pid:
            sources.append(f'playlist:{pid}')
    set_config('autofill_sources_json', _json.dumps(sources))

    interval = request.form.get('autofill_interval_minutes', '30')
    try:
        ival = max(5, min(360, int(interval)))
    except ValueError:
        ival = 30
    set_config('autofill_interval_minutes', str(ival))

    # B6: hot-reschedule the APScheduler job so interval changes take effect
    try:
        from flask import current_app
        sched = getattr(current_app, 'scheduler', None)
        if sched is not None:
            sched.reschedule_job('library_autofill', trigger='interval', minutes=ival)
    except Exception:
        log.exception('autofill: failed to reschedule library_autofill job')

    flash(
        f"Autofill {'ON' if enabled else 'OFF'} — "
        f"{len(sources)} source(s) enabled, every {ival} min.",
        'success' if enabled else 'info',
    )
    return redirect(url_for('main.library_autofill'))


@bp.route('/library-autofill/scan-now', methods=['POST'])
def library_autofill_scan_now():
    from . import autofill_engine
    if get_config('autofill_enabled', '0') != '1':
        flash('Autofill is OFF — flip the master toggle ON first.', 'warning')
        return redirect(url_for('main.library_autofill'))
    autofill_engine.trigger_immediate_tick()
    flash('Scan started — refresh in a minute to see results.', 'success')
    return redirect(url_for('main.library_autofill'))






@bp.route('/library-autofill/retry/<int:action_id>', methods=['POST'])
def library_autofill_retry(action_id):
    """B29: force a re-attempt on a single AutofillAction row (resets status to
    'pending' so the next tick treats it as new)."""
    from .db import AutofillAction
    with session_scope() as s:
        row = s.get(AutofillAction, action_id)
        if not row:
            flash('Action not found', 'error')
        else:
            row.status = 'pending_retry'
            row.last_attempt_at = datetime.utcnow().replace(year=2000)  # ancient -> immediate re-eligible
            row.note = (row.note or '') + ' [user forced retry]'
            flash(f'Will re-attempt {row.artist} - {row.album} on next tick', 'success')
    return redirect(url_for('main.library_autofill'))



# ====================================================================
# Settings — consolidated config page (Spotify, Plex, Lidarr, slskd, autofill)
# ====================================================================
@bp.route("/settings", methods=["GET"])
def settings_view():
    """One page to see + edit every config knob — now also hosts the (merged-in)
    Library Autofill controls. The old /library-autofill page redirects here."""
    import json as _json
    from .db import AutofillAction, SpotifyLikedTrack
    from datetime import datetime as _dt, timedelta as _td

    # ── Library Autofill context (merged from the old dedicated page) ──
    try:
        enabled_sources = _json.loads(get_config('autofill_sources_json') or _json.dumps(['liked']))
    except Exception:
        enabled_sources = ['liked']
    try:
        last_run = _json.loads(get_config('autofill_last_run_json') or '{}')
    except Exception:
        last_run = {}
    try:
        last_successful_run = _json.loads(get_config("autofill_last_successful_run_json") or "{}")
    except Exception:
        last_successful_run = {}
    # The Status card's "Plex coverage" was a STALE value frozen at the last *successful*
    # run (autofill keeps deferring on pipeline-busy, so it never recomputes). Compute it
    # live so the card shows the real current coverage instead of a days-old number.
    try:
        from .autofill_engine import _plex_match_coverage_pct
        _live_cov = _plex_match_coverage_pct(1)
        if isinstance(last_run, dict):
            last_run["plex_coverage_pct"] = _live_cov
        if isinstance(last_successful_run, dict):
            last_successful_run["plex_coverage_pct"] = _live_cov
    except Exception:
        log.exception("settings_view: live plex coverage compute failed")
    spotify_playlists = []
    if auth_spotify.is_authed():
        try:
            spotify_playlists = [
                {'id': p.id, 'name': p.name, 'track_count': p.track_count}
                for p in spotify_client.list_my_playlists(use_cache=True)
            ]
        except Exception:
            log.exception('settings_view: failed listing Spotify playlists')
    with session_scope() as s:
        total_queued = s.query(AutofillAction).filter(AutofillAction.status == 'queued').count()
        try:
            from .autofill_engine import get_current_acquisitions as _gca
            total_downloading = len(_gca())
        except Exception:
            total_downloading = s.query(AutofillAction).filter(AutofillAction.status == 'downloading').count()
        total_imported = s.query(AutofillAction).filter(AutofillAction.status == 'imported').count()
        total_library_existing = s.query(AutofillAction).filter(AutofillAction.status == 'library_existing').count()
        total_failed = s.query(AutofillAction).filter(
            AutofillAction.status.in_(['failed', 'lookup_empty', 'lookup_low_confidence', 'abandoned'])
        ).count()
        total_pending_retry = s.query(AutofillAction).filter(AutofillAction.status == 'pending_retry').count()
        _day_ago = _dt.utcnow() - _td(hours=24)
        total_queued_fresh = s.query(AutofillAction).filter(
            AutofillAction.status == 'queued', AutofillAction.last_attempt_at >= _day_ago).count()
        total_queued_stale = max(0, total_queued - total_queued_fresh)
        cache_count = s.query(SpotifyLikedTrack).count()
    try:
        _sync_last = _json.loads(get_config("spotify_liked_sync_last_json") or "{}")
    except Exception:
        _sync_last = {}

    # Plex's own report of where its music library lives — lets the Connections
    # card offer a pick-list instead of a paste-in path.
    plex_locations = []
    try:
        _srv = plex_client._connect()
        _sec = plex_client._music_section(_srv) if _srv else None
        if _sec:
            plex_locations = [str(x) for x in (_sec.locations or [])]
    except Exception:
        log.exception("settings_view: could not fetch Plex library locations")

    return render_template(
        "settings.html",
        spotify_ok=auth_spotify.is_authed(),
        self_repair_full=_self_repair_full(),
        self_repair_bypass=_self_repair_bypass(),
        container_name=os.environ.get("CONTAINER_NAME", "plexify"),
        bypass_prompt_pending=get_config("bypass_prompt_pending", ""),
        plex_locations=plex_locations,
        plex_health=plex_client.check_health(),
        lidarr_health={"connected": False, "root_folders": []},
        slskd_status=None,
        # merged Library Autofill context
        autofill_enabled=(get_config("autofill_enabled", "0") == "1"),
        enabled_sources=enabled_sources,
        spotify_playlists=spotify_playlists,
        autofill_interval=get_config("autofill_interval_minutes", "30"),
        spotify_cache_stats={"count": cache_count, "last_sync": _sync_last},
        liked_songs_cover=get_config("liked_songs_cover", "star"),
        last_run=last_run,
        last_successful_run=last_successful_run,
        total_queued=total_queued,
        total_downloading=total_downloading,
        total_imported=total_imported,
        total_library_existing=total_library_existing,
        total_failed=total_failed,
        total_pending_retry=total_pending_retry,
        total_queued_fresh=total_queued_fresh,
        total_queued_stale=total_queued_stale,
        config={
            "spotify_client_id": get_config("spotify_client_id", ""),
            "plex_url": get_config("plex_url", ""),
            "plex_section": get_config("plex_music_section_key", ""),
            "lidarr_url": "",
            "lidarr_api_key": "",
            "slskd_url": "",
            "slskd_api_key": "",
            "autofill_enabled": get_config("autofill_enabled", "0") == "1",
            "autofill_interval_minutes": get_config("autofill_interval_minutes", "30"),
            "autofill_max_new_per_tick": get_config("autofill_max_new_per_tick", "100"),
            "autofill_root_folder_path": get_config("autofill_root_folder_path", "/Volumes/MediaVolume3/plexify-music"),
            "autofill_quality_profile_name": get_config("autofill_quality_profile_name", ""),
        },
    )


@bp.route("/library")
def library_view():
    """Navigable grid of every album Plexify has completed into the library."""
    return render_template("library.html")


@bp.route("/library-autofill/star-liked-now", methods=["POST"])
def star_liked_now():
    """Manually run the Plexamp-star sync for a batch of liked songs."""
    from flask import jsonify
    from . import autofill_engine
    try:
        batch = int(request.form.get("batch", 300))
    except (TypeError, ValueError):
        batch = 300
    res = autofill_engine.star_liked_songs_in_plex_tick(batch=max(1, min(2000, batch)))
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(res)
    flash(f"Starred {res.get('starred', 0)} liked songs in Plex "
          f"(checked {res.get('checked', 0)}, {res.get('not_found', 0)} not in library yet).", "success")
    return redirect(url_for("main.settings_view"))


@bp.route("/settings/liked-cover", methods=["POST"])
def set_liked_cover_route():
    """Set the Spotify Liked Songs playlist poster (star|heart); keeps both available."""
    from flask import jsonify
    from . import liked_cover
    which = (request.form.get("which") or "star").lower()
    res = liked_cover.set_liked_songs_cover(which)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(res)
    if res.get("ok"):
        flash(f"Liked Songs cover set to the {which}.", "success")
    else:
        flash(f"Couldn't set cover: {res.get('error', 'unknown')}", "error")
    return redirect(url_for("main.settings_view"))


@bp.route("/settings/reconcile-stars", methods=["POST"])
def reconcile_stars_route():
    """Scan for / remove Plexamp stars on tracks that aren't Spotify Liked Songs."""
    from flask import jsonify
    from . import autofill_engine
    do_apply = (request.form.get("apply") == "1")
    res = autofill_engine.reconcile_liked_stars(apply=do_apply)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(res)
    if do_apply:
        flash(f"Removed stars from {res.get('unstarred', 0)} non-liked songs "
              f"(rollback: {res.get('manifest')}).", "success")
    else:
        flash(f"{res.get('to_unstar', 0)} of {res.get('rated', 0)} rated tracks aren't "
              f"Spotify-liked.", "warning")
    return redirect(url_for("main.settings_view"))


_CONDENSE = {"state": "idle"}
_CONDENSE_LOCK = threading.Lock()


def _condense_runner():
    """Run the album rulebook on demand until the library settles: rule 1
    (combine by cover identity), rules 2-4 (paren/shared-word/VA-soundtrack
    merges, one-version dedupe, monster re-home, fake-album ban), then reconcile
    the Plex tiles so the condensation shows up in Plex."""
    from . import autofill_engine as ae

    def setp(**kw):
        with _CONDENSE_LOCK:
            _CONDENSE.update(kw)

    tot = {"merged": 0, "deduped": 0, "rehomed": 0, "hidden": 0, "tiles": 0}
    try:
        setp(state="running", phase="Combining by cover art (rule 1)", **tot)
        for _ in range(6):
            try:
                ae.cover_identity_tick(merge_batch=25)
            except Exception:
                pass
        setp(phase="Merging, de-duping & splitting (rules 2-4)")
        for _ in range(18):
            try:
                r = ae.album_rules_tick(merge_batch=12, dedupe_batch=80, rehome_batch=3)
            except Exception:
                r = {}
            tot["merged"] += int(r.get("r2_merged", 0) or 0)
            tot["deduped"] += int(r.get("r3_atticed", 0) or 0)
            tot["rehomed"] += int(r.get("r4_rehomed", 0) or 0)
            tot["hidden"] += int(r.get("r2_hidden", 0) or 0)
            setp(**tot)
            if not any(r.get(k) for k in ("r2_merged", "r3_atticed", "r4_rehomed", "r2_hidden")):
                break
        setp(phase="Reconciling Plex tiles")
        # Cover the WHOLE library in one pass. The scheduled tick windows 400 artists at a time
        # off a rotating cursor (bounded cost every 2h), but on-demand Condense must be complete —
        # with a 400-window over a 1144-artist library the loop hit a 0-merge window and broke
        # before reaching Pink Floyd/Smiths/Beatles, so duplicate co-located tiles never merged
        # (found 2026-07-07). Reset the cursor and use an unbounded window.
        set_config("tile_reconcile_cursor", "0")
        for _ in range(3):
            try:
                tr = ae.plex_tile_reconcile_tick(window=1_000_000, max_merges=1_000_000)
            except Exception:
                tr = {}
            tot["tiles"] += int(tr.get("merged", 0) or 0)
            setp(**tot)
            if not tr.get("merged"):
                break
        setp(state="done", phase="Done")
    except Exception as e:
        setp(state="error", error=str(e)[:200])


@bp.route("/library/condense-now", methods=["POST"])
def library_condense_now():
    from flask import jsonify
    with _CONDENSE_LOCK:
        if _CONDENSE.get("state") == "running":
            return jsonify({"started": False, "state": "running"})
        _CONDENSE.clear()
        _CONDENSE.update({"state": "running", "phase": "Starting…",
                          "merged": 0, "deduped": 0, "rehomed": 0, "hidden": 0, "tiles": 0})
    threading.Thread(target=_condense_runner, daemon=True).start()
    return jsonify({"started": True, "state": "running"})


@bp.route("/library/condense-status")
def library_condense_status():
    from flask import jsonify
    with _CONDENSE_LOCK:
        return jsonify(dict(_CONDENSE))


@bp.route("/library/complete-now", methods=["POST"])
def library_complete_now():
    """Manually kick the album-completion sweeper for a batch of partial albums,
    closest-to-done first. Album downloads can take minutes, so this runs detached
    in a background thread and returns immediately — the Albums page reflects new
    tracks once they land (refresh, or the nightly/4-min sweeper keeps going)."""
    from flask import jsonify
    from . import autofill_engine
    import threading
    try:
        batch = int(request.form.get("batch", 5))
    except (TypeError, ValueError):
        batch = 5
    batch = max(1, min(20, batch))

    def _runner():
        try:
            autofill_engine.complete_albums_tick(batch=batch)
        except Exception:
            pass

    threading.Thread(target=_runner, daemon=True).start()
    msg = (f"Topping up the {batch} partial album(s) closest to done in the background — "
           f"refresh in a bit to see new tracks land.")
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"started": True, "batch": batch})
    flash(msg, "success")
    return redirect(url_for("main.library_view"))


@bp.route("/api/library/albums")
def api_library_albums():
    """Albums that have at least one downloaded song, classified by whether ALL of the
    album's songs are present (completed) or only some (incomplete). The real track total
    comes from the local Spotify catalog mirror."""
    from flask import jsonify, request
    from .db import AutofillAction, SpotifyAlbum
    from .autofill_engine import _normalize_for_key
    import json as _json
    try: offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError): offset = 0
    try: limit = max(1, min(60, int(request.args.get("limit", 24))))
    except (TypeError, ValueError): limit = 24
    q = (request.args.get("q", "") or "").strip()
    _filt = (request.args.get("filter") or "all").lower()
    _sort = (request.args.get("sort") or "recent").lower()
    _src = (request.args.get("source") or "").lower()
    try:
        from .autofill_engine import _get_liked_track_ids
        liked_ids = _get_liked_track_ids()
    except Exception:
        liked_ids = set()
    with session_scope() as s:
        # Mirror lookups for the album's REAL total track count (source of truth for
        # "has all its songs"): by Spotify album id, and by (artist, album) name.
        by_id, by_name = {}, {}
        for a in s.query(SpotifyAlbum).all():
            try:
                tt = int(a.total_tracks or 0)
            except Exception:
                tt = 0
            if tt <= 0:
                continue
            by_id[a.album_id] = tt
            by_name[(_normalize_for_key(a.album_artist or ""), _normalize_for_key(a.name or ""))] = tt

        # imported + the FINAL locked stage (locked albums were silently
        # missing from this page once the lock feature landed)
        try:
            _hidden_dirs = set(_json.load(open("/data/hidden_albums.json")))
        except Exception:
            _hidden_dirs = set()
        base = s.query(AutofillAction).filter(
            AutofillAction.status.in_(("imported", "complete_locked")),
            AutofillAction.imported_paths.isnot(None),
        )
        if _src in ("soulseek", "spotiflac", "squid", "telegram"):
            base = base.filter(AutofillAction.source.ilike(_src + "%"))
        if q:
            like = f"%{q}%"
            base = base.filter(AutofillAction.artist.ilike(like) | AutofillAction.album.ilike(like))
        rows = base.order_by(desc(AutofillAction.last_attempt_at)).all()

        classified = []
        for r in rows:
            try:
                paths = _json.loads(r.imported_paths or "[]")
            except Exception:
                paths = []
            have = len(paths)
            if have < 1:
                continue  # no songs → not an album yet
            if _hidden_dirs and _json and paths and __import__("os").path.dirname(paths[0]) in _hidden_dirs:
                continue  # hidden by library hygiene
            total = None
            fa = (r.foreign_album_id or "")
            if fa.startswith("sp:"):
                total = by_id.get(fa[3:])
            if total is None:
                total = by_name.get((_normalize_for_key(r.artist or ""), _normalize_for_key(r.album or "")))
            completed = (total is not None and have >= total)
            classified.append((r, have, total, completed))

        if _filt == "completed":
            classified = [c for c in classified if c[3]]
        elif _filt == "incomplete":
            classified = [c for c in classified if not c[3]]
        elif _filt == "locked":
            classified = [c for c in classified if c[0].status == "complete_locked"]
        # sort orders (query default = most recently touched first)
        if _sort == "artist":
            classified.sort(key=lambda c: ((c[0].artist or "").lower(), (c[0].album or "").lower()))
        elif _sort == "album":
            classified.sort(key=lambda c: ((c[0].album or "").lower(), (c[0].artist or "").lower()))
        elif _sort == "missing":
            classified.sort(key=lambda c: (c[3], (c[2] - c[1]) if (c[2] and not c[3]) else 10**6))
        elif _sort == "largest":
            classified.sort(key=lambda c: -c[1])
        total_count = len(classified)
        page = classified[offset:offset + limit]

        out_items = []
        for r, have, total, completed in page:
            is_liked = False
            try:
                is_liked = any(t in liked_ids for t in _json.loads(r.track_ids_json or "[]"))
            except Exception:
                pass
            out_items.append({
                "id": r.id, "artist": r.artist, "album": r.album,
                "track_count": have, "total_tracks": total, "completed": completed,
                "size_bytes": r.total_size_bytes or 0,
                "source": (r.source or "").lower(), "is_liked": is_liked,
                "locked": r.status == "complete_locked",
                "imported_at": (r.last_attempt_at.isoformat() + "Z") if r.last_attempt_at else None,
            })
    return jsonify({"items": out_items, "offset": offset, "limit": limit, "total": total_count})


@bp.route("/api/library/artists")
def api_library_artists():
    """Library grouped by artist (Library page's Artists view). Same classification
    and filters as /api/library/albums but aggregated, so the client gets EVERY
    artist in one call instead of being capped by the album page size (60)."""
    from flask import jsonify, request
    from .db import AutofillAction, SpotifyAlbum
    from .autofill_engine import _normalize_for_key
    import json as _json, os as _os
    q = (request.args.get("q", "") or "").strip()
    _filt = (request.args.get("filter") or "all").lower()
    _src = (request.args.get("source") or "").lower()
    with session_scope() as s:
        by_id, by_name = {}, {}
        for a in s.query(SpotifyAlbum).all():
            try:
                tt = int(a.total_tracks or 0)
            except Exception:
                tt = 0
            if tt <= 0:
                continue
            by_id[a.album_id] = tt
            by_name[(_normalize_for_key(a.album_artist or ""), _normalize_for_key(a.name or ""))] = tt
        try:
            _hidden_dirs = set(_json.load(open("/data/hidden_albums.json")))
        except Exception:
            _hidden_dirs = set()
        base = s.query(AutofillAction).filter(
            AutofillAction.status.in_(("imported", "complete_locked")),
            AutofillAction.imported_paths.isnot(None),
        )
        if _src in ("soulseek", "spotiflac", "squid", "telegram"):
            base = base.filter(AutofillAction.source.ilike(_src + "%"))
        if q:
            like = f"%{q}%"
            base = base.filter(AutofillAction.artist.ilike(like) | AutofillAction.album.ilike(like))
        groups = {}
        for r in base.all():
            try:
                paths = _json.loads(r.imported_paths or "[]")
            except Exception:
                paths = []
            have = len(paths)
            if have < 1:
                continue
            if _hidden_dirs and paths and _os.path.dirname(paths[0]) in _hidden_dirs:
                continue
            total = None
            fa = (r.foreign_album_id or "")
            if fa.startswith("sp:"):
                total = by_id.get(fa[3:])
            if total is None:
                total = by_name.get((_normalize_for_key(r.artist or ""), _normalize_for_key(r.album or "")))
            completed = (total is not None and have >= total)
            if _filt == "completed" and not completed:
                continue
            if _filt == "incomplete" and completed:
                continue
            if _filt == "locked" and r.status != "complete_locked":
                continue
            key = (r.artist or "?")
            g = groups.get(key)
            if g is None:
                g = groups[key] = {"artist": key, "albums": 0, "id": r.id, "_best": have, "completed_albums": 0}
            g["albums"] += 1
            if completed:
                g["completed_albums"] += 1
            if have > g["_best"]:
                g["_best"] = have
                g["id"] = r.id
        items = sorted(groups.values(), key=lambda gg: gg["artist"].lower())
        for g in items:
            g.pop("_best", None)
    return jsonify({"items": items, "total": len(items)})


@bp.route("/api/library/songs")
def api_library_songs():
    """Liked songs + the source each was acquired from + on-disk state — powers the
    Library page's Songs view, where each row gets a Dispute button."""
    from flask import jsonify, request
    from .db import AutofillAction, SpotifyLikedTrack, TrackMapping
    import json as _json
    try: offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError): offset = 0
    try: limit = max(1, min(100, int(request.args.get("limit", 40))))
    except (TypeError, ValueError): limit = 40
    q = (request.args.get("q", "") or "").strip()
    _filt = (request.args.get("filter") or "all").lower()
    try:
        from .autofill_engine import _load_disputes
        disputes = _load_disputes()
    except Exception:
        disputes = {}
    with session_scope() as s:
        tmap = {}
        for r in s.query(AutofillAction).filter(
                AutofillAction.status.in_(("imported", "complete_locked", "library_existing"))).all():
            try: tids = _json.loads(r.track_ids_json or "[]")
            except Exception: tids = []
            _has = bool((r.imported_paths or "").strip()) and (r.imported_paths or "[]") != "[]"
            for t in tids:
                if t not in tmap:
                    tmap[t] = (r.source, r.id, _has or r.status == "library_existing")
        try:
            mapped = {m.spotify_track_id for m in s.query(TrackMapping).filter(TrackMapping.plex_track_key.isnot(None)).all()}
        except Exception:
            mapped = set()
        base = s.query(SpotifyLikedTrack)
        if q:
            like = f"%{q}%"
            base = base.filter(SpotifyLikedTrack.title.ilike(like) | SpotifyLikedTrack.artist.ilike(like) | SpotifyLikedTrack.album.ilike(like))
        rows = []
        for lt in base.order_by(desc(SpotifyLikedTrack.cached_at)).all():
            tid = lt.spotify_track_id
            src, rid, ondisk = tmap.get(tid, (None, None, False))
            on_disk = bool(ondisk) or (tid in mapped)
            if _filt == "ondisk" and not on_disk: continue
            if _filt == "missing" and on_disk: continue
            if _filt == "disputed" and tid not in disputes: continue
            rows.append((lt, tid, src, rid, on_disk))
        total = len(rows)
        out = []
        for lt, tid, src, rid, on_disk in rows[offset:offset + limit]:
            out.append({"track_id": tid, "title": lt.title, "artist": lt.artist, "album": lt.album,
                        "source": (src or "").split(":")[0], "row_id": rid, "on_disk": on_disk,
                        "duration": getattr(lt, "duration_ms", None),
                        "added_at": (lt.added_at_spotify.isoformat() + "Z") if getattr(lt, "added_at_spotify", None) else None,
                        "disputed": tid in disputes, "disputed_sources": disputes.get(tid, [])})
    return jsonify({"items": out, "total": total, "offset": offset, "limit": limit})


@bp.route("/library/dispute", methods=["POST"])
def library_dispute():
    """Flag a liked song as the wrong audio: attic it, blacklist the source it came
    from for this track, and re-queue so it re-acquires from somewhere else."""
    from flask import jsonify, request
    data = request.get_json(silent=True) or request.form
    tid = (data.get("track_id") or "").strip()
    rid = data.get("row_id")
    fp = data.get("file_path")
    try:
        if tid:
            from .autofill_engine import dispute_song
            return jsonify(dispute_song(tid))
        if rid and fp:
            from .autofill_engine import dispute_file
            return jsonify(dispute_file(rid, fp))
        return jsonify({"ok": False, "error": "missing track_id or row_id+file_path"}), 400
    except Exception as e:
        log.exception("library_dispute failed")
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@bp.route("/api/library/album/<int:row_id>")
def api_library_album(row_id):
    """An album's tracks (from its files) + per-track dispute info — powers the in-app
    album detail view that opens when an album tile is clicked."""
    from flask import jsonify
    from .db import AutofillAction, SpotifyLikedTrack
    from .autofill_engine import _norm_title_key, _load_disputes
    import json as _json, os as _os
    try:
        from mutagen import File as _MF
    except Exception:
        _MF = None
    disputes = _load_disputes()
    with session_scope() as s:
        r = s.get(AutofillAction, row_id)
        if not r:
            return jsonify({"error": "not found"}), 404
        try: paths = _json.loads(r.imported_paths or "[]")
        except Exception: paths = []
        try: tids = _json.loads(r.track_ids_json or "[]")
        except Exception: tids = []
        liked_by_key = {}
        for t in tids:
            lt = s.get(SpotifyLikedTrack, t)
            if lt and lt.title:
                liked_by_key[_norm_title_key(lt.title)] = t
        tracks = []
        for p in paths:
            pn = p
            # Stored DB paths are NAS-absolute (/plexify-music/…); map them onto the Mac's
            # SMB mount so the files are readable here. (The earlier sed re-point had
            # rewritten the match-prefix to the destination, making this a no-op.)
            if pn.startswith("/plexify-music/"):
                pn = "/Volumes/MediaVolume3/plexify-music/" + pn[len("/plexify-music/"):]
            if not _os.path.isfile(pn):
                continue
            title = ""; trackno = 0; dur = 0
            if _MF:
                try:
                    m = _MF(pn, easy=True)
                    if m and m.tags:
                        title = (m.tags.get("title") or [""])[0]
                        try: trackno = int(str((m.tags.get("tracknumber") or ["0"])[0]).split("/")[0])
                        except Exception: trackno = 0
                    dur = int(getattr(_MF(pn).info, "length", 0) or 0)
                except Exception:
                    pass
            if not title:
                title = _os.path.splitext(_os.path.basename(pn))[0]
            tk = _norm_title_key(title)
            tid = liked_by_key.get(tk)
            tracks.append({"title": title, "track_no": trackno, "duration": dur,
                           "track_id": tid, "file_path": pn,
                           "disputed": bool(tid and tid in disputes)})
        tracks.sort(key=lambda x: (x["track_no"] or 9999, (x["title"] or "").lower()))
        return jsonify({"row_id": r.id, "artist": r.artist, "album": r.album,
                        "source": (r.source or "").split(":")[0], "cover_id": r.id,
                        "locked": r.status == "complete_locked", "tracks": tracks})





@bp.route("/library-autofill/bulk-retry", methods=["POST"])
def library_autofill_bulk_retry():
    """UX: reset every failed / lookup_empty / lookup_low_confidence / abandoned
    AutofillAction back to a state where the next tick re-attempts it."""
    from .db import AutofillAction
    statuses = ["failed", "lookup_empty", "lookup_low_confidence", "abandoned"]
    with session_scope() as s:
        rows = s.query(AutofillAction).filter(AutofillAction.status.in_(statuses)).all()
        n = 0
        for r in rows:
            r.status = "pending_retry"
            r.last_attempt_at = datetime.utcnow().replace(year=2000)
            r.note = (r.note or "") + " [user bulk retry]"
            n += 1
    flash(f"Queued {n} albums for re-attempt on next tick.", "success")
    return redirect(url_for("main.library_autofill"))



@bp.route("/library-autofill/save-quality", methods=["POST"])
def library_autofill_save_quality():
    """Switch autofill between strict FLAC and FLAC+MP3-320."""
    profile_id = request.form.get("autofill_quality_profile_id", "").strip()
    profile_name = request.form.get("autofill_quality_profile_name", "").strip()
    if not profile_id:
        flash("Pick a quality profile.", "error")
        return redirect(url_for("main.settings_view"))
    set_config("autofill_quality_profile_id", profile_id)
    if profile_name:
        set_config("autofill_quality_profile_name", profile_name)
    flash(f"Quality profile set to: {profile_name or profile_id}. "
          f"New autofill additions + re-searches will use this.", "success")
    return redirect(url_for("main.settings_view"))



@bp.route("/library-autofill/backfill-plex", methods=["POST"])
def library_autofill_backfill_plex():
    """B5: backfill Plex matches for LocalTrack rows that don't yet have a
    plex_track_key. Without this, autofill considers most tracks 'missing'.
    Runs the matcher against the Plex library asynchronously."""
    from .db import LocalTrack, TrackMapping
    from . import sync_engine
    import threading

    job_id = jobs.create("backfill_plex_matcher", title="Backfill Plex matches", total=0)

    def runner():
        jobs.start(job_id, step="finding unmatched Spotify tracks")
        try:
            with session_scope() as s:
                # tracks present in LocalTrack but no Plex match in TrackMapping
                unmatched = list(s.scalars(
                    select(LocalTrack)
                    .where(~LocalTrack.spotify_track_id.in_(
                        select(TrackMapping.spotify_track_id)
                        .where(TrackMapping.plex_track_key.isnot(None))))
                    .limit(2000)  # safety cap
                ).all())
                snapshot = [(r.spotify_track_id, r.title, r.artist, r.album, r.duration_ms or 0) for r in unmatched]
            jobs.progress(job_id, total=len(snapshot), current=0)
            jobs.emit(job_id, f"checking {len(snapshot)} unmatched tracks against Plex")
            found = 0
            for i, (sid, title, artist, album, dur) in enumerate(snapshot, 1):
                if i % 25 == 0:
                    jobs.progress(job_id, current=i,
                                  step=f"{i}/{len(snapshot)} ({found} matched)")
                # _resolve_to_plex saves the mapping on hit + records search attempt on miss
                try:
                    class _T:
                        pass
                    _t = _T()
                    _t.id, _t.title, _t.artist, _t.album = sid, title, artist, album
                    _t.isrc, _t.duration_ms = None, dur
                    pkey = sync_engine._resolve_to_plex(_t)
                    if pkey:
                        found += 1
                except Exception:
                    pass
            jobs.finish(job_id, status="ok",
                        message=f"matched {found} new tracks to Plex",
                        result={"checked": len(snapshot), "matched": found})
        except Exception as e:
            log.exception("backfill plex matcher failed")
            jobs.fail(job_id, str(e))

    threading.Thread(target=runner, daemon=True).start()
    if request.headers.get("Accept", "").startswith("application/json") or request.args.get("json"):
        return jsonify({"job_id": job_id})
    flash(f"Started backfill (job #{job_id})", "success")
    return redirect(url_for("main.library_autofill"))



@bp.route("/library-autofill/picker-run", methods=["POST"])
def library_autofill_picker_run():
    """Manually trigger the smart picker for ONE album right now."""
    from . import autofill_engine
    import threading
    job_id = jobs.create("picker_run", title="Smart picker — manual run", total=1)
    def runner():
        jobs.start(job_id, step="picker_tick")
        try:
            res = autofill_engine.picker_tick()
            jobs.finish(job_id, status="ok",
                        message=f"picker ran: picked={res.get('picked')} "
                                f"target={res.get('target','?')[:60]} "
                                f"error={res.get('error','')[:200]}",
                        result=res)
        except Exception as e:
            log.exception("picker manual run failed")
            jobs.fail(job_id, str(e))
    threading.Thread(target=runner, daemon=True).start()
    if request.headers.get("Accept", "").startswith("application/json") or request.args.get("json"):
        return jsonify({"job_id": job_id})
    flash(f"Smart picker started (job #{job_id})", "success")
    return redirect(url_for("main.library_autofill"))


@bp.route("/library-autofill/save-picker-toggle", methods=["POST"])
def library_autofill_save_picker_toggle():
    enabled = request.form.get("autofill_picker_enabled") == "on"
    set_config("autofill_picker_enabled", "1" if enabled else "0")
    strict = request.form.get("autofill_strict_flac") == "on"
    set_config("autofill_strict_flac", "1" if strict else "0")
    mp3fb = request.form.get("autofill_allow_mp3_fallback") == "on"
    set_config("autofill_allow_mp3_fallback", "1" if mp3fb else "0")
    flash(f"Picker {'ON' if enabled else 'OFF'} · "
          f"FLAC-strict={'ON' if strict else 'OFF'} · "
          f"MP3 fallback={'ON' if mp3fb else 'OFF'}", "success")
    return redirect(url_for("main.settings_view"))



@bp.route("/library-autofill/save-mode", methods=["POST"])
def library_autofill_save_mode():
    mode = (request.form.get("autofill_acquisition_mode") or "album").strip().lower()
    if mode not in ("song", "album", "discography"):
        flash("Invalid acquisition mode.", "error")
        return redirect(url_for("main.settings_view"))
    set_config("autofill_acquisition_mode", mode)
    label = {"song": "Song only", "album": "Whole album",
             "discography": "Full artist discography"}[mode]
    flash(f"Acquisition mode set: {label}. New autofill ticks will use this.", "success")
    return redirect(url_for("main.settings_view"))



@bp.route("/library-autofill/mode-sync-preview", methods=["POST"])
def apply_mode_to_library_dry_run():
    """Dry-run: see what would get pruned without touching files."""
    from . import autofill_engine
    target = (request.form.get("autofill_acquisition_mode") or
              get_config("autofill_acquisition_mode", "album")).lower()
    res = autofill_engine.apply_mode_to_library(dry_run=True, new_mode=target)
    # Stash so the next render can show it
    import json as _json
    set_config("autofill_mode_sync_preview_json", _json.dumps(res))
    flash(
        f"Preview for mode '{target}': would prune {res['files_pruned']} files "
        f"({res['bytes_pruned'] // 1024 // 1024} MB) from {res['rows_pruned']} albums. "
        "Click 'Apply' below to actually move.", "info",
    )
    return redirect(url_for("main.settings_view"))


@bp.route("/library-autofill/mode-sync-apply", methods=["POST"])
def apply_mode_to_library_apply():
    """Execute: actually move out-of-scope files to /Volumes/MediaVolume3/plexify-music/__autofill_pruned/."""
    from . import autofill_engine
    target = (request.form.get("autofill_acquisition_mode") or
              get_config("autofill_acquisition_mode", "album")).lower()
    res = autofill_engine.apply_mode_to_library(dry_run=False, new_mode=target)
    flash(
        f"Pruned {res['files_pruned']} files ({res['bytes_pruned'] // 1024 // 1024} MB) "
        f"to {res.get('pruned_to_dir')}. You can rm -rf that dir whenever you're confident.",
        "success",
    )
    return redirect(url_for("main.settings_view"))



@bp.route("/library-autofill/sync-spotify-now", methods=["POST"])
def library_autofill_sync_spotify_now():
    from . import autofill_engine
    try:
        res = autofill_engine.sync_spotify_liked_tracks_tick()
        if res.get("errors"):
            flash(f"Sync partial: {res.get('new',0)} new, {res.get('updated',0)} updated, "
                  f"{res.get('removed',0)} removed. Hit Spotify rate-limit mid-pull — "
                  f"will retry next tick. ({len(res['errors'])} errors)", "warning")
        else:
            flash(f"Spotify sync complete: {res.get('new',0)} new, {res.get('updated',0)} updated, "
                  f"{res.get('removed',0)} removed liked tracks.", "success")
    except Exception as e:
        flash(f"Sync failed: {e}", "error")
    return redirect(url_for("main.library_autofill"))


# ====================================================================
# Dashboard API — Phase 1 (dashboard redesign)
# ====================================================================

@bp.route("/api/dashboard/health")
def api_dashboard_health():
    from flask import jsonify
    import json as _json
    services = {}
    # Spotify
    try:
        if auth_spotify.is_authed():
            from .db import SpotifyLikedTrack
            with session_scope() as s:
                cache_count = s.query(SpotifyLikedTrack).count()
            last_run = _json.loads(get_config("autofill_last_run_json") or "{}")
            errs = last_run.get("source_errors") or []
            rate_limited = any("429" in str(e) for e in errs)
            services["spotify"] = {
                "state": "yellow" if rate_limited else "green",
                "detail": f"{cache_count} tracks cached" + (" — rate-limited" if rate_limited else "")
            }
        else:
            services["spotify"] = {"state": "red", "detail": "not authed"}
    except Exception as e:
        services["spotify"] = {"state": "red", "detail": str(e)[:80]}
    # Plex
    try:
        h = plex_client.check_health()
        if h.get("connected"):
            tc = h.get("track_count") or 0
            services["plex"] = {"state": "green", "detail": f"{tc} FLAC tracks indexed"}
        else:
            services["plex"] = {"state": "red", "detail": "unreachable"}
    except Exception as e:
        services["plex"] = {"state": "red", "detail": str(e)[:80]}
    # SpotiFLAC — green when it has delivered in the last hour; yellow if it
    # hasn't (could be idle OR degraded). Always surfaces the installed version.
    try:
        from . import autofill_engine as _ae
        counts = _ae.get_provider_success_counts_1h()
        sf = int(counts.get("spotiflac", 0) or 0)
        try:
            ver = _ae.get_spotiflac_version()
        except Exception:
            ver = "?"
        if sf > 0:
            services["spotiflac"] = {"state": "green",
                                     "detail": f"working — {sf} import{'s' if sf != 1 else ''}/1h · {ver}"}
        else:
            services["spotiflac"] = {"state": "yellow",
                                     "detail": f"no imports in last hour · {ver}"}
    except Exception as e:
        services["spotiflac"] = {"state": "red", "detail": str(e)[:80]}
    # Soulseek — from slskd login state, plus its 1h delivery count for context.
    try:
        from . import slskd_client as _slc, autofill_engine as _ae2
        sh = _slc.check_health()
        sl = int(_ae2.get_provider_success_counts_1h().get("soulseek", 0) or 0)
        sl_ctx = f" · {sl} import{'s' if sl != 1 else ''}/1h"
        if sh.get("logged_in"):
            who = sh.get("username") or "connected"
            services["soulseek"] = {"state": "green", "detail": f"working — logged in as {who}{sl_ctx}"}
        elif sh.get("connected"):
            services["soulseek"] = {"state": "yellow", "detail": f"reachable, not logged in{sl_ctx}"}
        else:
            err = sh.get("error") or "unreachable"
            services["soulseek"] = {"state": "red", "detail": f"down — {err}"}
    except Exception as e:
        services["soulseek"] = {"state": "red", "detail": str(e)[:80]}
    # Telegram — third source (@BeatSpotBot, per-track FLAC). Green when it has
    # delivered in the last hour; yellow when configured-but-idle or enabled-but-unset;
    # unknown(grey) when disabled. Mirrors the spotiflac/soulseek pill format.
    try:
        from . import telegram_picker as _tg, autofill_engine as _ae3
        tcfg = _tg._cfg()
        tn = int(_ae3.get_provider_success_counts_1h().get("telegram", 0) or 0)
        if not tcfg["enabled"]:
            services["telegram"] = {"state": "unknown", "detail": "disabled"}
        elif not (tcfg["api_id"] and tcfg["api_hash"] and tcfg["session"]):
            services["telegram"] = {"state": "yellow", "detail": "enabled — needs setup in Settings"}
        else:
            bot = tcfg["bot"] or "@BeatSpotBot"
            if tn > 0:
                services["telegram"] = {"state": "green",
                                        "detail": f"working — {tn} import{'s' if tn != 1 else ''}/1h · {bot}"}
            else:
                services["telegram"] = {"state": "yellow", "detail": f"no imports in last hour · {bot}"}
    except Exception as e:
        services["telegram"] = {"state": "red", "detail": str(e)[:80]}
    # squid.wtf — fourth source (Qobuz hi-res via squid.wtf). Green when it delivered in the
    # last hour; yellow when idle; red while on a rate-limit cooldown; grey when disabled.
    try:
        from . import squid_adapter as _sq, autofill_engine as _ae4
        sn = int(_ae4.get_provider_success_counts_1h().get("squid", 0) or 0)
        if not _sq.is_enabled():
            services["squid"] = {"state": "unknown", "detail": "disabled"}
        elif _sq._in_break():
            services["squid"] = {"state": "red", "detail": "on cooldown (rate-limited)"}
        elif sn > 0:
            services["squid"] = {"state": "green", "detail": f"working — {sn} import{'s' if sn != 1 else ''}/1h · Qobuz hi-res"}
        else:
            services["squid"] = {"state": "yellow", "detail": "ready — no imports in last hour · Qobuz hi-res"}
    except Exception as e:
        services["squid"] = {"state": "red", "detail": str(e)[:80]}
    # Enrich the four SOURCE pills with structured fields for the hover-expand popover.
    try:
        from . import autofill_engine as _aeh
        _counts = _aeh.get_provider_success_counts_1h()
    except Exception:
        _counts = {}
    _src_meta = {
        "soulseek":  ("Soulseek",  1, "Free peer-to-peer (slskd) — searches connected users. Variable quality/availability; tried first so the paid mirrors aren't hit unnecessarily."),
        "squid":     ("squid.wtf", 2, "Qobuz hi-res (24-bit lossless) via squid.wtf's JSON API — no browser or captcha. Recovered 12/12 songs the other sources couldn't."),
        "spotiflac": ("SpotiFLAC", 3, "Mirror scrapers across Qobuz / Tidal / Deezer / Amazon. Broadest catalogue but the most rate-limit-prone."),
        "telegram":  ("Telegram",  4, "@BeatSpotBot — per-track FLAC. Also reuses already-delivered files straight from chat history, so it works even while the bot is on a break."),
    }
    for _k, (_nm, _ord, _about) in _src_meta.items():
        if _k in services:
            services[_k]["name"] = _nm
            services[_k]["order"] = _ord
            services[_k]["about"] = _about
            services[_k]["imports_1h"] = int(_counts.get(_k, 0) or 0)
    # Rollup
    # Overall reflects CORE infra (Spotify + Plex). Source health (spotiflac/soulseek) is
    # shown separately and must not flip the status dot yellow just because a source is idle.
    _core = [services[k]["state"] for k in ("spotify", "plex") if k in services]
    overall = "red" if "red" in _core else ("yellow" if "yellow" in _core else "green")
    return jsonify({"overall": overall, "services": services})


@bp.route("/api/dashboard/live")
def api_dashboard_live():
    from flask import jsonify
    from .db import AutofillAction
    from . import autofill_engine
    out = {"downloading": None, "idle": True, "queue_depth": 0, "next_picker_tick_in_seconds": None}
    try:
        with session_scope() as s:
            out["queue_depth"] = s.query(AutofillAction).filter(AutofillAction.status == "queued").count()
    except Exception:
        pass
    try:
        from .main import scheduler
        job = scheduler.get_job("library_autofill_picker")
        if job and job.next_run_time:
            import datetime as _dt
            now = _dt.datetime.now(job.next_run_time.tzinfo) if job.next_run_time.tzinfo else _dt.datetime.utcnow()
            out["next_picker_tick_in_seconds"] = max(0, int((job.next_run_time - now).total_seconds()))
    except Exception:
        pass
    acqs = autofill_engine.get_current_acquisitions()
    import datetime as _dt_live
    _now_live = _dt_live.datetime.utcnow()
    from .db import SpotifyLikedTrack as _SLT
    def _song_names_for_row(rid, limit=8):
        """Liked-song titles for a row's tracks — so 'Right now' shows what's landing."""
        if not rid:
            return []
        try:
            import json as _j
            with session_scope() as s:
                row = s.get(AutofillAction, rid)
                if not row:
                    return []
                tids = _j.loads(row.track_ids_json or "[]")
                names = []
                for t in tids[:limit]:
                    lt = s.get(_SLT, t)
                    if lt and lt.title:
                        names.append(lt.title)
                return names
        except Exception:
            return []
    def _enrich(a):
        d = {
            "artist": a.get("artist"),
            "album": a.get("album"),
            "spotify_url": a.get("spotify_url"),
            "started_at": a.get("started_at"),
            "row_id": a.get("row_id"),
            "mode": a.get("mode"),
            "tracks_done": a.get("tracks_done", 0) or 0,
            "tracks_total": a.get("tracks_total", 0) or 0,
            "quality_target": a.get("quality_target"),
            "quality_acquired": a.get("quality_acquired"),
            "source": a.get("source"),  # 'soulseek' | 'spotiflac' | None — live chip
        }
        # Upgrade-in-progress: re-acquiring an album that's already imported, at a higher
        # target quality than it currently has → the live strip shows an "Upgrading" tag.
        try:
            from .db import AutofillAction as _AA
            with session_scope() as _us:
                _row = _us.get(_AA, a.get("row_id"))
                _prev_imported = bool(_row and _row.imported_paths)
                _prev_q = ((_row.quality_acquired if _row else "") or "").upper()
            d["upgrading"] = bool(_prev_imported
                                  and "HI_RES" in (a.get("quality_target") or "").upper()
                                  and "HI_RES" not in _prev_q)
        except Exception:
            d["upgrading"] = False
        elapsed = None
        try:
            st = a.get("started_at")
            if st:
                t0 = _dt_live.datetime.fromisoformat(st.rstrip("Z"))
                elapsed = max(0, int((_now_live - t0).total_seconds()))
        except Exception:
            elapsed = None
        d["elapsed_seconds"] = elapsed
        eta = None
        total = d["tracks_total"] or 0
        done = d["tracks_done"] or 0
        if total > 0 and elapsed is not None:
            if done > 0:
                per_track = elapsed / done
            else:
                per_track = 15.0
            eta = max(0, int((total - done) * per_track))
        d["eta_seconds"] = eta
        try:
            d["song_names"] = _song_names_for_row(a.get("row_id"))
        except Exception:
            d["song_names"] = []
        return d
    out["downloads"] = [_enrich(a) for a in acqs]
    if acqs:
        out["downloading"] = out["downloads"][0]  # bw-compat: single most-recent
        out["idle"] = False
    # Terminal outcomes in the last ~30s — drives the success/fail departure
    # animations for rows that just left "Right now".
    try:
        out["recent_outcomes"] = autofill_engine.get_recent_outcomes(within_s=30)
    except Exception:
        out["recent_outcomes"] = []
    return jsonify(out)


def _audio_info(path):
    """Best-effort technical specs for an audio file: (codec, bits, rate_hz, length_s,
    size_bytes). Read only for the small paginated page, so the mutagen cost is bounded."""
    import os as _o
    try:
        size = _o.path.getsize(path)
    except Exception:
        size = 0
    ext = _o.path.splitext(path)[1].lower()
    bits = rate = 0
    length = 0.0
    codec = (ext.lstrip(".").upper() or "FILE")
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC
            i = FLAC(path).info
            bits = int(getattr(i, "bits_per_sample", 0) or 0)
            rate = int(getattr(i, "sample_rate", 0) or 0)
            length = float(getattr(i, "length", 0) or 0)
            codec = "FLAC"
        elif ext in (".m4a", ".alac", ".aac"):
            from mutagen.mp4 import MP4
            i = MP4(path).info
            rate = int(getattr(i, "sample_rate", 0) or 0)
            bits = int(getattr(i, "bits_per_sample", 0) or 0)
            length = float(getattr(i, "length", 0) or 0)
            codec = "ALAC" if "alac" in str(getattr(i, "codec", "")).lower() else "AAC"
        elif ext == ".mp3":
            from mutagen.mp3 import MP3
            i = MP3(path).info
            rate = int(getattr(i, "sample_rate", 0) or 0)
            length = float(getattr(i, "length", 0) or 0)
            codec = "MP3"
    except Exception:
        pass
    return codec, bits, rate, length, size


def _audio_title(path):
    """Embedded TITLE tag (the real song name). Used so the feed shows 'Reptilia'
    rather than the filename, which for Telegram-sourced files is a bare ISRC code."""
    import os as _o
    try:
        ext = _o.path.splitext(path)[1].lower()
        if ext == ".flac":
            from mutagen.flac import FLAC
            t = FLAC(path).get("title")
        elif ext in (".m4a", ".alac", ".aac"):
            from mutagen.mp4 import MP4
            t = MP4(path).get("\xa9nam")
        elif ext == ".mp3":
            from mutagen.easyid3 import EasyID3
            t = EasyID3(path).get("title")
        else:
            return ""
        return (str(t[0]).strip() if t else "")
    except Exception:
        return ""


# A bare ISRC filename (e.g. USRC10301520.flac) is the @BeatSpotBot signature — no other
# source names files this way — so it's a reliable per-file "this came from Telegram" signal.
_ISRC_NAME_RE = __import__("re").compile(r"^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$")


def _quality_tier(codec, bits, rate):
    """Returns (tier, label). tier ∈ {hires, cd, lossless, lossy}."""
    def _khz(hz):
        k = hz / 1000.0
        return ("%g" % k)
    if codec in ("MP3", "AAC"):
        return "lossy", (codec + (" · %s kHz" % _khz(rate) if rate else ""))
    # Hi-Res = better-than-CD: 24-bit at any rate, OR 16-bit above 48 kHz.
    if (bits >= 24) or (bits and rate > 48000):
        return "hires", "%d-bit / %s kHz" % (bits, _khz(rate))
    if bits and rate:
        return "cd", "%d-bit / %s kHz" % (bits, _khz(rate))
    return "lossless", codec


_FEED_LIKED_CACHE = {"data": None, "ts": 0.0}


def _feed_liked_lookup():
    """Cached (90s) global Spotify-liked lookup: (core_title_set, (core_title,artist)_set).
    A recently-added song shows a heart ONLY when BOTH its normalized title AND artist match a
    liked song. (core_titles is retained for callers but is no longer sufficient on its own —
    title-alone matching produced false hearts on same-named covers by other artists.)"""
    import time as _t
    now = _t.time()
    if _FEED_LIKED_CACHE["data"] is not None and (now - _FEED_LIKED_CACHE["ts"]) < 90:
        return _FEED_LIKED_CACHE["data"]
    try:
        from .autofill_engine import _build_liked_lookup
        with session_scope() as s:
            _k, _p, _t2, core_pairs, core_titles = _build_liked_lookup(s)
        data = (core_titles, core_pairs)
    except Exception:
        data = (set(), set())
    _FEED_LIKED_CACHE["data"] = data
    _FEED_LIKED_CACHE["ts"] = now
    return data


# Reward-feed stat caches — placed files never change, but the feed used to re-stat up to
# 250 albums × 60 paths on EVERY poll. Over LAN that's milliseconds; over the Tailscale SMB
# mount (away from home) it's thousands of WAN round-trips per poll — the feed took >175s
# and starved gunicorn's worker threads (found 2026-07-06). mtimes cache forever (mtime is
# fixed at placement), misses re-check after a TTL, audio specs cache forever (immutable).
_FEED_MTIME_CACHE: dict = {}
_FEED_MISS_CACHE: dict = {}      # path -> monotonic time of last failed stat
_FEED_INFO_CACHE: dict = {}      # path -> (title, codec, bits, rate, length, size)
_FEED_SRC_CACHE: dict = {}       # path -> stamped source tag ("" when absent) — a mutagen
                                 # header-read per path was the loop's REAL per-poll cost
_FEED_MISS_TTL = 600.0


@bp.route("/api/dashboard/reward-feed")
def api_dashboard_reward_feed():
    from flask import jsonify, request
    from .db import AutofillAction
    import json as _json
    try: offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError): offset = 0
    try: limit = max(1, min(50, int(request.args.get("limit", 10))))
    except (TypeError, ValueError): limit = 10
    import os as _os_rf, re as _re_rf
    from datetime import datetime as _dt_rf
    def _mk(_s):
        return _re_rf.sub(r'[^a-z0-9]', '', (_s or '').lower())
    # FLAT, per-SONG feed ordered by when each file actually landed on disk (mtime).
    # We do NOT group by album: a track added to an existing album surfaces on its own
    # instead of re-floating the whole album with its full song list. (Album song-counts
    # live on the Albums page.) We scan a window of recently-touched imported albums,
    # then sort their individual files by mtime.
    core_titles, core_pairs = _feed_liked_lookup()
    try:
        from .autofill_engine import _core_ta as _cta, _norm_ta as _nta, _get_source_tag as _src_tag
    except Exception:
        _cta = _nta = _mk
        _src_tag = lambda _p: ""
    flat = []
    with session_scope() as s:
        rows = list(s.scalars(
            select(AutofillAction)
            .where(AutofillAction.status == "imported")
            .where(AutofillAction.imported_paths.isnot(None))
            .order_by(desc(AutofillAction.last_attempt_at))
            .limit(100)
        ).all())
        for r in rows:
            try:
                paths = _json.loads(r.imported_paths or "[]")
            except Exception:
                paths = []
            if not paths:
                continue
            note_low = (r.note or "").lower()
            src = (getattr(r, "source", None) or "").strip().lower()
            src_detail = (getattr(r, "source_detail", None) or "").strip()
            if src not in ("soulseek", "spotiflac"):
                if "soulseek" in note_low:
                    src = "soulseek"
                elif any(k in note_low for k in ("spotiflac", "qobuz", "tidal", "deezer", "amazon")):
                    src = "spotiflac"
                elif "sweep" in note_low:
                    src = "sweep"
                else:
                    src = "import"
            _artist_low = (r.artist or "").strip().lower()
            for _p in paths[:60]:
                _mt = _FEED_MTIME_CACHE.get(_p)
                if _mt is None:
                    import time as _t_rf
                    _missed = _FEED_MISS_CACHE.get(_p)
                    if _missed is not None and _t_rf.monotonic() - _missed < _FEED_MISS_TTL:
                        continue  # known-missing — don't re-stat over SMB every poll
                    try:
                        _mt = _os_rf.path.getmtime(_p)
                        _FEED_MTIME_CACHE[_p] = _mt
                        _FEED_MISS_CACHE.pop(_p, None)
                    except Exception:
                        _FEED_MISS_CACHE[_p] = _t_rf.monotonic()
                        continue  # file gone → skip
                _basename_noext = _os_rf.path.splitext(_os_rf.path.basename(_p))[0].strip()
                # Per-FILE source: an album row carries ONE source, but a mixed album can have
                # tracks from different sources (e.g. most from Soulseek, a few completion-filled
                # from Telegram). A bare-ISRC filename means Telegram regardless of the row source.
                # Exact per-file provenance: the stamped source tag wins; fall back to the
                # old heuristic (bare-ISRC name → telegram) only for pre-tag legacy files.
                # Cached — the stamped tag never changes, and reading it is a mutagen
                # header-read per path (an SMB round-trip that dominated every poll).
                _stag = _FEED_SRC_CACHE.get(_p)
                if _stag is None:
                    try:
                        _stag = _src_tag(_p) or ""
                    except Exception:
                        _stag = ""
                    _FEED_SRC_CACHE[_p] = _stag
                _file_src = _stag or ("telegram" if _ISRC_NAME_RE.match(_basename_noext) else src)
                _b = _basename_noext.strip('_').strip()
                _b = _re_rf.sub(r'^.*?\s+-\s+\d{1,3}\s+-\s+', '', _b)          # "Album - 03 - Title"
                _b = _re_rf.sub(r'^\s*\d{1,3}\s*[-._)]?\s+', '', _b)           # leading track number
                _b = _re_rf.sub(r'_\d{6,}$', '', _b)                           # soulseek hash suffix
                _b = _re_rf.sub(r'\s*\((?:FLAC|MP3|WAV|ALAC)[^)]*\)\s*$', '', _b, flags=_re_rf.IGNORECASE)
                _b = _re_rf.sub(r'\s*\([^)]*(?:bit|kbps|khz)[^)]*\)\s*$', '', _b, flags=_re_rf.IGNORECASE)
                _b = _b.strip().strip('_').strip()
                if _artist_low and _b.lower().endswith(" - " + _artist_low):
                    _b = _b[:-(len(_artist_low) + 3)].strip()
                elif _artist_low and _b.lower().startswith(_artist_low + " - "):
                    _b = _b[len(_artist_low) + 3:].strip()
                if not _b:
                    continue
                _ck = _cta(_b)
                # Heart ONLY when BOTH normalized title AND artist match a liked song. Title-alone
                # matching lit false hearts on same-named covers by other artists (e.g. The Osmonds
                # "Don't Panic" vs liked Coldplay "Don't Panic"; BYU Vocal Point "Happy" vs Pharrell).
                liked = bool(_ck) and (_ck, _nta(r.artist or "")) in core_pairs
                flat.append({
                    "uid": f"{r.id}:{_mk(_b)}",
                    "id": r.id, "artist": r.artist, "album": r.album,
                    "song": _b, "name": _b,
                    "liked": liked, "is_liked": liked,
                    "source": _file_src, "source_detail": src_detail,
                    "was_upgraded": bool(getattr(r, "was_upgraded", False)),
                    "_mt": _mt, "_path": _p,
                    "imported_at": _dt_rf.utcfromtimestamp(_mt).isoformat() + "Z",
                })
    flat.sort(key=lambda x: x["_mt"], reverse=True)
    page = flat[offset:offset + limit]
    # Enrich ONLY the page (bounded) with real audio specs — bit depth / sample rate /
    # Hi-Res vs CD / codec / size — the audiophile detail for Recently Added.
    for _it in page:
        # Prefer the embedded TITLE tag for the display name — it's the real song title and
        # matches Plex. Filename-derived names are a fallback (and are a bare ISRC code for
        # Telegram-sourced files, which is the "named a number" bug).
        # Audio specs are immutable per placed file — cache so repeat polls don't
        # re-read FLAC headers over the (possibly WAN) SMB mount.
        _pth = _it.get("_path")
        _cached = _FEED_INFO_CACHE.get(_pth)
        if _cached is None:
            _cached = (_audio_title(_pth), *_audio_info(_pth))
            _FEED_INFO_CACHE[_pth] = _cached
        _title, codec, bits, rate, length, size = _cached
        if _title:
            _it["name"] = _title
            _it["song"] = _title
        tier, qlabel = _quality_tier(codec, bits, rate)
        _it["codec"] = codec
        _it["bits"] = bits
        _it["sample_rate"] = rate
        _it["quality_tier"] = tier
        _it["quality_label"] = qlabel
        _it["size_bytes"] = size
        _it["duration_s"] = int(round(length))
        _it.pop("_mt", None)
        _it.pop("_path", None)
    return jsonify({"items": page, "offset": offset, "limit": limit})


TELEMETRY_PATH = "/data/ui_telemetry.jsonl"


@bp.route("/api/telemetry", methods=["POST"])
def api_telemetry():
    """Append a batch of UI-interaction events (clicks, scrolls, page views, dwell) to a
    JSONL log so future analysis can guide UI improvements. Fast + never errors the UI."""
    import json as _j, os as _o, time as _t
    try:
        data = request.get_json(force=True, silent=True) or {}
        events = data.get("events")
        if not isinstance(events, list):
            return ("", 204)
        srv_ms = int(_t.time() * 1000)
        lines = []
        for ev in events[:300]:
            if not isinstance(ev, dict):
                continue
            ev["srv_t"] = srv_ms
            try:
                lines.append(_j.dumps(ev, ensure_ascii=False, separators=(",", ":"))[:2000])
            except Exception:
                continue
        if lines:
            try:
                if _o.path.exists(TELEMETRY_PATH) and _o.path.getsize(TELEMETRY_PATH) > 25 * 1024 * 1024:
                    _o.replace(TELEMETRY_PATH, TELEMETRY_PATH + ".1")
            except Exception:
                pass
            with open(TELEMETRY_PATH, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
    except Exception:
        pass
    return ("", 204)


@bp.route("/api/telemetry/summary")
def api_telemetry_summary():
    """Aggregated view of the UI telemetry — top clicked elements, dead clicks, pages,
    and scroll-depth reach — so usage patterns are actionable at a glance."""
    from flask import jsonify
    import json as _j, os as _o
    from collections import Counter, defaultdict
    clicks = Counter()
    dead = Counter()
    pages = Counter()
    submits = Counter()
    scroll_reach = defaultdict(list)
    dwell = defaultdict(list)
    total = 0
    try:
        if _o.path.exists(TELEMETRY_PATH):
            with open(TELEMETRY_PATH, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = _j.loads(line)
                    except Exception:
                        continue
                    total += 1
                    ty = e.get("type")
                    if ty == "pageview":
                        pages[e.get("p") or "?"] += 1
                    elif ty == "click":
                        lbl = (e.get("txt") or e.get("id") or e.get("cls") or e.get("tag") or "?")[:50]
                        clicks[lbl] += 1
                        if e.get("dead"):
                            dead[(e.get("p") or "?") + " :: " + lbl] += 1
                    elif ty == "submit":
                        submits[e.get("action") or e.get("id") or "?"] += 1
                    elif ty in ("scroll", "dwell"):
                        m = e.get("max") if ty == "scroll" else e.get("max_scroll")
                        if m is not None:
                            scroll_reach[e.get("p") or "?"].append(int(m))
                        if ty == "dwell" and e.get("secs") is not None:
                            dwell[e.get("p") or "?"].append(int(e.get("secs")))
    except Exception:
        pass
    def _avg(xs):
        return round(sum(xs) / len(xs), 1) if xs else None
    return jsonify({
        "total_events": total,
        "top_clicks": clicks.most_common(30),
        "dead_clicks": dead.most_common(20),
        "pageviews": pages.most_common(20),
        "form_submits": submits.most_common(20),
        "avg_scroll_reach_pct": {k: _avg(v) for k, v in scroll_reach.items()},
        "avg_dwell_secs": {k: _avg(v) for k, v in dwell.items()},
    })


@bp.route("/settings/connections", methods=["POST"])
def save_connections():
    """Save service endpoints + credentials (Settings -> Connections). Blank
    credential fields keep the saved value so users can edit URLs without
    re-pasting secrets."""
    _full = _self_repair_full()
    for k in ("slskd_url", "lidarr_url", "plex_library_path"):
        if k in request.form:
            set_config(k, (request.form.get(k) or "").strip().rstrip("/"))
    # break-risky settings — changeable with Self-repair armed (FULL) or with the
    # external-AI bypass: bypass saves the value AND hands you a repair prompt.
    _bypass = _self_repair_bypass()
    for k in ("squid_base", "spotiflac_repo"):
        if k in request.form:
            _newv = (request.form.get(k) or "").strip().rstrip("/")
            _oldv = get_config(k, "")
            if _full or _bypass:
                set_config(k, _newv)
                if _bypass and not _full and _newv != _oldv and _oldv:
                    set_config("bypass_prompt_pending", _bypass_prompt(k, _oldv, _newv))
                    flash("Saved — copy the repair prompt below into your AI app to finish the change.", "warning")
            else:
                flash("Risky setting ignored: enable Self-repair (FULL) or the external-AI bypass to change it.", "warning")
    for k in ("slskd_api_key", "lidarr_api_key"):
        v = (request.form.get(k) or "").strip()
        if v:
            set_config(k, v)
    flash("Connections saved.", "success")
    return redirect(url_for("main.settings_view"))


def _self_repair_bypass() -> bool:
    """True when the user opted to use an EXTERNAL AI (Claude app, etc.) instead
    of the built-in key: risky fields unlock, and saving one generates a
    copy/paste prompt telling that AI how to adapt the code to the new value."""
    return get_config("self_repair_bypass", "0") == "1"


def _bypass_prompt(key: str, old: str, new: str) -> str:
    """The copy/paste brief for the user's external AI after a risky change."""
    import os as _os
    _root = _os.environ.get("HOST_PROJECT_DIR") or "<your Plexify project directory>"
    _cname = _os.environ.get("CONTAINER_NAME") or "plexify"
    head = (
        "I run Plexify, a Spotify->Plex FLAC sync app (Flask + Docker) on my NAS.\n"
        f"Project root: {_root} — container name: {_cname}.\n"
        "Redeploy after code changes with:\n"
        f"  cd {_root} && docker compose up -d --build\n\n"
        f"I just changed the break-risky setting `{key}` from `{old}` to `{new}` in the\n"
        "web UI (the config value is ALREADY saved — only the code needs to catch up).\n\n"
    )
    if key == "telegram_bot":
        body = (
            "Task: app/telegram_picker.py drives the OLD bot — its request commands and\n"
            f"reply-parsing (captions, filenames, buttons) are tuned to {old}.\n"
            f"Interact with {new} from the saved user session to learn its request format\n"
            "and reply shape, then adapt telegram_picker.py to it.\n\n"
            "Verify: Settings -> Telegram -> Test session, then watch a picker run\n"
            f"(docker logs -f {_cname}, look for telegram_picker lines) deliver a FLAC."
        )
    elif key == "squid_base":
        body = (
            "Task: app/squid_adapter.py expects a squid.wtf-style JSON API at the base\n"
            "URL (see its module docstring for the endpoints + response fields it uses).\n"
            f"Probe {new}'s actual API (search + download endpoints), then adapt\n"
            "_api_search / _resolve / _download_url to its paths and response shape.\n\n"
            "Verify in-container:\n"
            f"  docker exec {_cname} python3 -c \"import sys; sys.path.insert(0,'/app');\n"
            "  from app import squid_adapter as q; print(q.search_best('Daft Punk','Get Lucky'))\""
        )
    elif key == "spotiflac_repo":
        body = (
            f"Task: the nightly updater now installs git+https://github.com/{new}.git@main\n"
            "as the `SpotiFLAC` package. Install it in the container, confirm\n"
            "`import SpotiFLAC` works, and check every call point in\n"
            "app/spotiflac_adapter.py still matches the fork's API; adapt the adapter\n"
            "where signatures or module paths differ.\n\n"
            "Verify: Settings -> SpotiFLAC -> 'Update SpotiFLAC now' completes, the\n"
            "Installed version changes, and a picker run produces files."
        )
    else:
        body = f"Task: find every place the code depends on the old value `{old}` and adapt it to `{new}`."
    return head + body


def _self_repair_full() -> bool:
    """True when the AI self-repair layer is armed (toggle ON + API key saved).
    Gates the break-risky settings: with repair armed, Plexify can fix itself
    if a risky change breaks a source adapter."""
    return (get_config("smart_update_enabled", "0") == "1"
            and bool((get_config("anthropic_api_key", "") or "").strip()))


@bp.route("/settings/bypass-prompt/dismiss", methods=["POST"])
def dismiss_bypass_prompt():
    set_config("bypass_prompt_pending", "")
    return redirect(url_for("main.settings_view"))


@bp.route("/settings/downloading", methods=["POST"])
def save_downloading():
    """ONE save for the whole Downloading section: master switches, feeds,
    per-song scope, and quality rules (merged from four legacy per-card routes)."""
    import json as _json
    # master switches
    set_config('autofill_enabled', '1' if request.form.get('autofill_enabled') == 'on' else '0')
    set_config('autofill_picker_enabled', '1' if request.form.get('autofill_picker_enabled') == 'on' else '0')
    # feeds
    sources: list[str] = []
    if request.form.get('source_liked') == 'on':
        sources.append('liked')
    if request.form.get('source_followed_artists') == 'on':
        sources.append('followed_artists')
    for pid in request.form.getlist('source_playlist'):
        if pid:
            sources.append(f'playlist:{pid}')
    set_config('autofill_sources_json', _json.dumps(sources))
    # per-song scope
    mode = (request.form.get("autofill_acquisition_mode") or "album").strip().lower()
    if mode in ("song", "album", "discography"):
        set_config("autofill_acquisition_mode", mode)
    # quality rules
    set_config("autofill_strict_flac", "1" if request.form.get("autofill_strict_flac") == "on" else "0")
    set_config("autofill_allow_mp3_fallback", "1" if request.form.get("autofill_allow_mp3_fallback") == "on" else "0")
    was_cd = get_config("autofill_allow_cd_quality", "0")
    now_cd = "1" if request.form.get("autofill_allow_cd_quality") else "0"
    set_config("autofill_allow_cd_quality", now_cd)
    if now_cd == "1" and was_cd != "1":
        # newly enabled: give abandoned rows another shot under the new policy
        from .db import AutofillAction
        from sqlalchemy import update as _update
        with session_scope() as s_:
            n = s_.execute(
                _update(AutofillAction)
                .where(AutofillAction.status == "abandoned")
                .values(status="queued", attempt_count=0,
                        note="re-queued under CD-quality fallback policy")
            ).rowcount
        flash(f"Downloading settings saved. CD-quality fallback enabled — {n} abandoned albums re-queued.", "success")
    else:
        flash("Downloading settings saved.", "success")
    return redirect(url_for("main.settings_view"))


@bp.route("/settings/telegram", methods=["POST"])
def save_telegram():
    """Save Telegram source settings. Blank api_hash/session are left untouched so the
    user can edit other fields without re-pasting secrets."""
    from flask import jsonify
    set_config("telegram_enabled", "1" if request.form.get("telegram_enabled") in ("1", "on", "true") else "0")
    for k in ("telegram_api_id",):
        if k in request.form:
            set_config(k, (request.form.get(k) or "").strip())
    if "telegram_bot" in request.form:
        _newv = (request.form.get("telegram_bot") or "").strip()
        _oldv = get_config("telegram_bot", "@BeatSpotBot")
        if _self_repair_full() or _self_repair_bypass():
            set_config("telegram_bot", _newv)
            if _self_repair_bypass() and not _self_repair_full() and _newv != _oldv and _oldv:
                set_config("bypass_prompt_pending", _bypass_prompt("telegram_bot", _oldv, _newv))
                flash("Saved — copy the repair prompt on the Settings page into your AI app to finish the change.", "warning")
        else:
            flash("Bot username unchanged: enable Self-repair (FULL) or the external-AI bypass to change this risky setting.", "warning")
    for k in ("telegram_api_hash", "telegram_session"):
        v = (request.form.get(k) or "").strip()
        if v:
            set_config(k, v)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True})
    flash("Telegram source settings saved.", "success")
    return redirect(url_for("main.settings_view"))


@bp.route("/settings/smart-update", methods=["POST"])
def save_smart_update():
    """Save the Anthropic API key + smart-update settings. Blank api_key is left untouched."""
    from flask import jsonify
    set_config("smart_update_enabled", "1" if request.form.get("smart_update_enabled") in ("1", "on", "true") else "0")
    set_config("self_repair_bypass", "1" if request.form.get("self_repair_bypass") in ("1", "on", "true") else "0")
    model = (request.form.get("anthropic_model") or "").strip()
    if model:
        set_config("anthropic_model", model)
    key = (request.form.get("anthropic_api_key") or "").strip()
    if key:
        set_config("anthropic_api_key", key)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True})
    flash("Smart update settings saved.", "success")
    return redirect(url_for("main.settings_view"))


@bp.route("/settings/soulseek", methods=["POST"])
def save_soulseek():
    """Save Soulseek source settings — slskd connection + the smart hi-res gate.
    A blank API key keeps the saved one."""
    from flask import jsonify
    set_config("autofill_soulseek_hires_only",
               "1" if request.form.get("soulseek_hires_only") in ("1", "on", "true") else "0")
    if "slskd_url" in request.form:
        set_config("slskd_url", (request.form.get("slskd_url") or "").strip().rstrip("/"))
    _k = (request.form.get("slskd_api_key") or "").strip()
    if _k:
        set_config("slskd_api_key", _k)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True})
    flash("Soulseek settings saved.", "success")
    return redirect(url_for("main.settings_view"))


@bp.route("/api/smart-update/test", methods=["POST"])
def test_smart_update():
    """Validate the saved Anthropic API key with one tiny call."""
    from flask import jsonify
    try:
        from . import smart_update
        res = smart_update.check_key()
    except Exception as e:
        res = {"ok": False, "error": str(e)[:200]}
    return jsonify(res)


@bp.route("/api/telegram/test", methods=["POST"])
def test_telegram():
    """Verify the Telegram session is authorized + the bot is reachable."""
    from flask import jsonify
    try:
        from . import telegram_picker
        res = telegram_picker._run(telegram_picker.check_session())
    except Exception as e:
        res = {"ok": False, "error": str(e)[:200]}
    return jsonify(res)


@bp.route("/api/album-art/<int:autofill_action_id>")
def api_album_art(autofill_action_id):
    # album-art: disk-first — picker now saves cover.jpg in each album's /Volumes/MediaVolume3/plexify-music dir.
    # Read it directly before any Plex roundtrip or DB cache lookup.
    from flask import send_file
    from .db import AutofillAction
    import os as _os, json as _jjson
    try:
        with session_scope() as s:
            row = s.get(AutofillAction, autofill_action_id)
            if row and row.imported_paths:
                try:
                    paths = _jjson.loads(row.imported_paths)
                    if paths:
                        album_dir = _os.path.dirname(paths[0])
                        cover = _os.path.join(album_dir, "cover.jpg")
                        if _os.path.isfile(cover):
                            return send_file(cover, mimetype="image/jpeg",
                                             max_age=86400)
                except Exception:
                    pass
    except Exception:
        log.exception("album-art disk lookup raised")
    # Fallback to Plex (original behavior follows)
    from flask import Response, abort
    from .db import AutofillAction, AlbumArtCache
    from datetime import datetime, timedelta
    import requests
    with session_scope() as s:
        row = s.get(AutofillAction, autofill_action_id)
        if not row:
            abort(404)
        ak, bk = row.artist_key or "", row.album_key or ""
        cached = s.scalar(select(AlbumArtCache).where(AlbumArtCache.artist_key == ak).where(AlbumArtCache.album_key == bk))
        if cached and (datetime.utcnow() - cached.fetched_at) < timedelta(days=30):
            return Response(cached.image_bytes, mimetype=cached.content_type, headers={"Cache-Control": "public, max-age=2592000"})
    try:
        srv = plex_client._connect()
        if not srv:
            abort(404)
        music = next((sec for sec in srv.library.sections() if sec.type == "artist"), None)
        if not music:
            abort(404)
        from .autofill_engine import _normalize_for_key
        match = None
        for artist in music.searchArtists(title=row.artist):
            if _normalize_for_key(artist.title) == ak:
                for alb in artist.albums():
                    if _normalize_for_key(alb.title) == bk:
                        match = alb; break
                if match: break
        if not match or not match.thumb:
            abort(404)
        url = srv.url(match.thumb, includeToken=True)
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        body = resp.content
        ct = resp.headers.get("Content-Type", "image/jpeg")
        with session_scope() as s:
            existing = s.scalar(select(AlbumArtCache).where(AlbumArtCache.artist_key == ak).where(AlbumArtCache.album_key == bk))
            if existing:
                existing.image_bytes = body; existing.content_type = ct; existing.fetched_at = datetime.utcnow()
            else:
                s.add(AlbumArtCache(artist_key=ak, album_key=bk, image_bytes=body, content_type=ct, fetched_at=datetime.utcnow()))
        return Response(body, mimetype=ct, headers={"Cache-Control": "public, max-age=2592000"})
    except Exception as _e:
        from werkzeug.exceptions import HTTPException as _HE
        if isinstance(_e, _HE):
            raise  # a 404 for an album Plex has no art for is expected — don't log a trace
        log.exception("album_art failed for id=%d", autofill_action_id)
        abort(404)


@bp.route("/api/album-go/<int:autofill_action_id>")
def api_album_go(autofill_action_id):
    from flask import redirect, abort
    from .db import AutofillAction
    from .autofill_engine import _normalize_for_key
    import urllib.parse as _up
    with session_scope() as s:
        row = s.get(AutofillAction, autofill_action_id)
        if not row: abort(404)
        artist_name = row.artist or ""
        album_name = row.album or ""
        ak, bk = row.artist_key or "", row.album_key or ""

    def _plex_search_url(srv):
        q = _up.quote((artist_name + " " + album_name).strip())
        return f"{srv._baseurl}/web/index.html#!/search?query={q}"

    try:
        srv = plex_client._connect()
        if not srv:
            abort(404)
        music = next((sec for sec in srv.library.sections() if sec.type == "artist"), None)
        if music:
            for artist in music.searchArtists(title=artist_name):
                if _normalize_for_key(artist.title) == ak:
                    for alb in artist.albums():
                        if _normalize_for_key(alb.title) == bk:
                            return redirect(f"{srv._baseurl}/web/index.html#!/server/{srv.machineIdentifier}/details?key={alb.key}", code=302)
        # No exact album match → a clickable item must still LEAD somewhere: land on a
        # Plex search for this artist+album rather than 404.
        return redirect(_plex_search_url(srv), code=302)
    except Exception:
        log.exception("album_go failed for id=%d", autofill_action_id)
        try:
            srv = plex_client._connect()
            if srv:
                return redirect(_plex_search_url(srv), code=302)
        except Exception:
            pass
        abort(404)


@bp.route("/api/playlist-go/<int:pair_id>")
def api_playlist_go(pair_id):
    """Open the Plex counterpart of a mirrored playlist (or a Plex search fallback)."""
    from flask import redirect, abort
    from .db import PlaylistPair
    import urllib.parse as _up
    with session_scope() as s:
        pair = s.get(PlaylistPair, pair_id)
        if not pair or not pair.plex_playlist_key:
            abort(404)
        key, name = pair.plex_playlist_key, pair.name
    try:
        srv = plex_client._connect()
        if not srv:
            abort(404)
        try:
            pl = srv.fetchItem(int(key))
            return redirect(f"{srv._baseurl}/web/index.html#!/server/{srv.machineIdentifier}/playlist?key={_up.quote('/playlists/' + str(pl.ratingKey), safe='')}", code=302)
        except Exception:
            return redirect(f"{srv._baseurl}/web/index.html#!/search?query={_up.quote(name or '')}", code=302)
    except Exception:
        log.exception("playlist_go failed for pair %d", pair_id)
        abort(404)



@bp.route("/library-autofill/save-allow-cd", methods=["POST"])
def library_autofill_save_allow_cd():
    """Toggle whether abandoned rows can retry at CD-quality."""
    enabled = "1" if request.form.get("autofill_allow_cd_quality") else "0"
    set_config("autofill_allow_cd_quality", enabled)
    if enabled == "1":
        # Reset abandoned rows back to queued for retry under the new policy
        from .db import AutofillAction
        from sqlalchemy import update
        with session_scope() as s:
            n = s.execute(
                update(AutofillAction)
                .where(AutofillAction.status == "abandoned")
                .values(status="queued", attempt_count=0, note="re-queued under CD-quality fallback policy")
            ).rowcount
        flash(f"CD-quality fallback enabled. {n} abandoned rows re-queued for retry.", "success")
    else:
        flash("CD-quality fallback disabled. Hi-res-only standard restored.", "info")
    return redirect(url_for("main.library_autofill"))



# ── Playlist ZIP download — bundle the playlist\'s in-library FLACs ─────────────
def _resolve_playlist_files(pair_id):
    """(files, total_bytes, name, available, total_tracks). files = list of
    (local_path, arcname, size) for every playlist track whose file is on disk."""
    import os as _os, re as _re
    from .autofill_engine import _plex_prefix, _LOCAL_PATH_PREFIX
    with session_scope() as _s:
        pair = _s.get(PlaylistPair, pair_id)
        if not pair:
            return [], 0, "", 0, 0
        name = pair.name or "playlist"
        pkey = pair.plex_playlist_key
    if not pkey:
        return [], 0, name, 0, 0
    srv = plex_client._connect()
    if not srv:
        return [], 0, name, 0, 0
    try:
        items = srv.fetchItem(int(pkey)).items()
    except Exception:
        return [], 0, name, 0, 0
    def _safe(x):
        return _re.sub(r'[/\\:*?"<>|]', "-", (x or "").strip())[:80]
    files, total_tracks = [], 0
    for i, t in enumerate(items, 1):
        total_tracks += 1
        try:
            fpath = t.media[0].parts[0].file
        except Exception:
            continue
        local = fpath.replace(_plex_prefix(), _LOCAL_PATH_PREFIX, 1)
        if not _os.path.isfile(local):
            continue
        ext = _os.path.splitext(local)[1] or ".flac"
        artist = getattr(t, "originalTitle", None) or getattr(t, "grandparentTitle", "") or ""
        title = getattr(t, "title", "") or _os.path.splitext(_os.path.basename(local))[0]
        arc = "%03d - %s - %s%s" % (i, _safe(artist) or "Unknown", _safe(title) or "track", ext)
        try:
            sz = _os.path.getsize(local)
        except OSError:
            sz = 0
        files.append((local, arc, sz))
    return files, sum(z for _, _, z in files), name, len(files), total_tracks


def _zip_cap_bytes():
    try:
        return int(float(get_config("playlist_zip_max_gb", "20") or "20") * (1024 ** 3))
    except Exception:
        return 20 * (1024 ** 3)


@bp.route("/api/playlists/<int:pair_id>/zip-info")
def api_playlist_zip_info(pair_id):
    files, total, name, available, total_tracks = _resolve_playlist_files(pair_id)
    cap = _zip_cap_bytes()
    return jsonify({"name": name, "available": available, "total": total_tracks,
                    "bytes": total, "gb": round(total / (1024 ** 3), 2),
                    "cap_gb": round(cap / (1024 ** 3), 1), "over": total > cap})


# Background ZIP builds — write the archive into a NAS folder; report its path.
_ZIP_JOBS = {}
_ZIP_LOCK = threading.Lock()
_ZIP_DIR = "/Volumes/MediaVolume3/Downloads/music/playlist-zips"   # container path; host = ${DOWNLOADS_DIR}/playlist-zips


def _zip_host_path(container_path):
    """Translate the container zip path to the path the user sees on the NAS."""
    dl = os.environ.get("DOWNLOADS_DIR", "")
    if dl and container_path.startswith("/Volumes/MediaVolume3/Downloads/music"):
        return dl.rstrip("/") + container_path[len("/Volumes/MediaVolume3/Downloads/music"):]
    return container_path


def _expire_zip(pair_id, path):
    """15-min TTL backstop: delete the zip if it's still sitting there unused."""
    with _ZIP_LOCK:
        job = _ZIP_JOBS.get(pair_id)
        if not job or job.get("state") != "done" or job.get("path") != path:
            return
        job["state"] = "expired"
        job.pop("timer", None)
    try:
        os.remove(path)
    except OSError:
        pass


def _sweep_stale_zips():
    """Delete any zip/.part older than 15 min (orphans from an app restart)."""
    import time as _t
    try:
        for fn in os.listdir(_ZIP_DIR):
            if not (fn.endswith(".zip") or fn.endswith(".part")):
                continue
            fp = os.path.join(_ZIP_DIR, fn)
            try:
                if _t.time() - os.path.getmtime(fp) > 900:
                    os.remove(fp)
            except OSError:
                pass
    except OSError:
        pass


def _build_zip_worker(pair_id):
    import re as _re, zipfile as _zip
    try:
        files, total, name, available, _tt = _resolve_playlist_files(pair_id)
        if not files:
            with _ZIP_LOCK:
                _ZIP_JOBS[pair_id] = {"state": "error", "error": "no songs in your library yet", "done": 0, "total": 0}
            return
        if total > _zip_cap_bytes():
            with _ZIP_LOCK:
                _ZIP_JOBS[pair_id] = {"state": "error", "done": 0, "total": available,
                    "error": "%.1f GB exceeds the %.0f GB limit" % (total / 1024 ** 3, _zip_cap_bytes() / 1024 ** 3)}
            return
        os.makedirs(_ZIP_DIR, exist_ok=True)
        _sweep_stale_zips()
        safe = _re.sub(r"[^\w \-]", "_", name).strip() or "playlist"
        cpath = os.path.join(_ZIP_DIR, safe + ".zip")
        part = cpath + ".part"
        with _ZIP_LOCK:
            _ZIP_JOBS[pair_id] = {"state": "building", "done": 0, "total": available, "bytes": total,
                                  "path": cpath, "host_path": _zip_host_path(cpath), "error": None}
        with _zip.ZipFile(part, "w", _zip.ZIP_STORED, allowZip64=True) as zf:
            for n, (fp, arc, _sz) in enumerate(files, 1):
                try:
                    zf.write(fp, arc)
                except Exception:
                    pass
                with _ZIP_LOCK:
                    if pair_id in _ZIP_JOBS:
                        _ZIP_JOBS[pair_id]["done"] = n
        os.replace(part, cpath)
        timer = threading.Timer(900, _expire_zip, args=(pair_id, cpath))
        timer.daemon = True
        with _ZIP_LOCK:
            j = _ZIP_JOBS.get(pair_id) or {}
            j.update({"state": "done", "path": cpath, "host_path": _zip_host_path(cpath),
                      "timer": timer, "expires_in": 900})
            try:
                j["size_gb"] = round(os.path.getsize(cpath) / 1024 ** 3, 2)
            except OSError:
                pass
            _ZIP_JOBS[pair_id] = j
        timer.start()
    except Exception as e:
        with _ZIP_LOCK:
            _ZIP_JOBS[pair_id] = {"state": "error", "error": str(e)[:200], "done": 0, "total": 0}


@bp.route("/api/playlists/<int:pair_id>/zip-build", methods=["POST"])
def api_playlist_zip_build(pair_id):
    with _ZIP_LOCK:
        cur = _ZIP_JOBS.get(pair_id)
        if cur and cur.get("state") == "building":
            return jsonify({"state": "building", "done": cur.get("done", 0), "total": cur.get("total", 0)})
        _ZIP_JOBS[pair_id] = {"state": "building", "done": 0, "total": 0, "error": None}
    threading.Thread(target=_build_zip_worker, args=(pair_id,), daemon=True).start()
    return jsonify({"state": "building", "started": True})


@bp.route("/api/playlists/<int:pair_id>/zip-status")
def api_playlist_zip_status(pair_id):
    with _ZIP_LOCK:
        cur = _ZIP_JOBS.get(pair_id)
        if cur:
            cur = {k: v for k, v in cur.items() if k != "timer"}
    if cur and cur.get("state") == "done" and cur.get("path") and not os.path.isfile(cur["path"]):
        cur = dict(cur); cur["state"] = "downloaded"
    return jsonify(cur or {"state": "idle"})


@bp.route("/api/playlists/<int:pair_id>/preview")
def api_playlist_preview(pair_id):
    """Return the playlist's tracks with Plex-availability flag.
    Used by the expand-row UI on /playlists to show what's in the playlist
    and what's not on Plex yet (greyed out)."""
    from flask import jsonify, request
    from .db import LocalTrack, TrackMapping, PlaylistPair
    try:
        limit = max(1, min(500, int(request.args.get("limit", 100) or 100)))
        offset = max(0, int(request.args.get("offset", 0) or 0))
    except (ValueError, TypeError):
        limit, offset = 100, 0
    with session_scope() as s:
        pair = s.get(PlaylistPair, pair_id)
        if not pair:
            return jsonify({"error": "pair not found"}), 404
        tracks = list(s.scalars(
            select(LocalTrack).where(LocalTrack.pair_id == pair_id)
            .order_by(LocalTrack.position)
            .offset(offset).limit(limit)
        ).all())
        if not tracks:
            return jsonify({"items": [], "total": 0, "offset": offset, "limit": limit,
                            "name": pair.name})
        # Lookup in-Plex status for these tracks
        track_ids = [t.spotify_track_id for t in tracks]
        plex_keys = {}
        # Also look up AutofillAction.status for these track IDs (mapping via track_ids_json)
        # NOTE: AutofillAction.track_ids_json is a JSON list; SQL LIKE search is cheaper than parsing all
        acq_status_by_tid = {}
        from .db import AutofillAction
        import json as _json
        for chunk_i in range(0, len(track_ids), 500):
            chunk = track_ids[chunk_i:chunk_i + 500]
            for tm in s.scalars(
                select(TrackMapping).where(TrackMapping.spotify_track_id.in_(chunk))
            ).all():
                plex_keys[tm.spotify_track_id] = tm.plex_track_key
        # AutofillAction status: pre-load all rows that have a track_ids_json containing any of our tids
        # (this scans the table but the table is small — under 5k rows)
        all_aa = list(s.scalars(select(AutofillAction)).all())
        tid_set = set(track_ids)
        for aa in all_aa:
            if not aa.track_ids_json:
                continue
            try:
                aa_tids = _json.loads(aa.track_ids_json)
            except Exception:
                continue
            for tid in aa_tids:
                if tid in tid_set:
                    # Earlier/more-specific status wins (imported > downloading > queued > abandoned)
                    cur = acq_status_by_tid.get(tid)
                    rank_order = {"imported": 5, "downloading": 4, "library_existing": 3, "queued": 2, "abandoned": 1}
                    if cur is None or rank_order.get(aa.status, 0) > rank_order.get(cur, 0):
                        acq_status_by_tid[tid] = aa.status
        total = s.query(LocalTrack).filter(LocalTrack.pair_id == pair_id).count()
        items = []
        for t in tracks:
            pkey = plex_keys.get(t.spotify_track_id)
            items.append({
                "position": t.position,
                "title": t.title or "",
                "artist": t.artist or "",
                "album": t.album or "",
                "spotify_track_id": t.spotify_track_id,
                "in_plex": bool(pkey),
                "acq_status": acq_status_by_tid.get(t.spotify_track_id),
                "added_at": (t.added_at.isoformat() + "Z") if t.added_at else None,
            })
    return jsonify({
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "name": pair.name,
    })



@bp.route("/library-autofill/save-concurrency", methods=["POST"])
def library_autofill_save_concurrency():
    """Deprecated: download concurrency is now AUTO-TUNED (AIMD) at runtime — there is no
    manual slots knob. Kept so any old client/bookmark gets a clean answer."""
    from flask import jsonify
    try:
        from .autofill_engine import _smart_concurrency
        cur = _smart_concurrency()
    except Exception:
        cur = None
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True, "auto": True, "current": cur})
    flash("Download concurrency is automatic now — it tunes itself to the sources.", "success")
    return redirect(url_for("main.settings_view"))



@bp.route("/library-autofill/save-source-priority", methods=["POST"])
def library_autofill_save_source_priority():
    """Persist the user's drag-ordered download-source priority.

    Body: JSON {"order": ["soulseek","spotiflac"]} (preferred, sent by the
    Settings drag list) or form field `order=soulseek,spotiflac`.
    Writes autofill_soulseek_primary ('1' if soulseek is first) which the
    picker already honors, plus autofill_source_priority_json for the record.
    """
    from flask import jsonify
    import json as _json
    order = None
    try:
        if request.is_json:
            order = (request.get_json(silent=True) or {}).get("order")
    except Exception:
        order = None
    if not order:
        raw = request.form.get("order", "")
        order = [x for x in raw.split(",") if x]
    order = [str(x).strip().lower() for x in (order or [])
             if str(x).strip().lower() in ("soulseek", "spotiflac")]
    # de-dup, preserve order
    _seen = set()
    order = [x for x in order if not (x in _seen or _seen.add(x))]
    # ensure both present (append any missing; default soulseek-first)
    for _s in ("soulseek", "spotiflac"):
        if _s not in order:
            order.append(_s)
    soulseek_primary = "1" if order[0] == "soulseek" else "0"
    set_config("autofill_soulseek_primary", soulseek_primary)
    set_config("autofill_source_priority_json", _json.dumps(order))
    log.info("source-priority saved: %s (soulseek_primary=%s)", order, soulseek_primary)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True, "order": order, "soulseek_primary": soulseek_primary})
    flash("Source priority saved: " + " → ".join(order), "success")
    return redirect(url_for("main.settings_view"))


@bp.route("/api/picker/status", methods=["GET"])
def api_picker_status():
    """Return picker scheduler state for dashboard controls."""
    from flask import jsonify
    from datetime import datetime, timezone
    paused = False
    next_in = None
    max_inst = None
    try:
        from .main import scheduler
        job = scheduler.get_job("library_autofill_picker")
        if job is not None:
            max_inst = getattr(job, "max_instances", None)
            nxt = getattr(job, "next_run_time", None)
            if nxt is None:
                paused = True
            else:
                delta = (nxt - datetime.now(timezone.utc)).total_seconds()
                next_in = max(0, int(delta))
    except Exception:
        log.exception("api_picker_status: scheduler introspection failed")
    in_flight = 0
    queue_depth = 0
    try:
        from .autofill_engine import CURRENT_ACQUISITIONS, _ACQUISITION_LOCK
        with _ACQUISITION_LOCK:
            in_flight = len(CURRENT_ACQUISITIONS)
    except Exception:
        pass
    try:
        from .db import SessionLocal, AutofillAction
        with SessionLocal() as s:
            queue_depth = s.query(AutofillAction).filter(AutofillAction.status == "queued").count()
    except Exception:
        log.exception("api_picker_status: queue_depth count failed")
    # Surface circuit-breaker cooldown state for dashboard banner.
    cooldown = {"in_cooldown": False, "until": None, "seconds_remaining": 0}
    try:
        from .autofill_engine import is_picker_in_cooldown
        cooldown = is_picker_in_cooldown()
    except Exception:
        pass
    # Surface tick interval too (auto-derived from max_instances)
    tick_interval = None
    try:
        from .main import scheduler as _sched
        _job = _sched.get_job("library_autofill_picker")
        if _job and hasattr(_job.trigger, "interval"):
            tick_interval = int(_job.trigger.interval.total_seconds())
    except Exception:
        pass
    streaming_paused = False
    streaming_sessions = 0
    conc_ceiling = None
    try:
        from . import autofill_engine as _aep
        if get_config("autofill_pause_when_streaming", "1") == "1":
            streaming_sessions = int(_aep._plex_active_video_sessions() or 0)
            streaming_paused = streaming_sessions > 0
        try:
            conc_ceiling = int(_aep._conc_ceiling())
        except Exception:
            conc_ceiling = None
    except Exception:
        pass
    # Plex match coverage (two cheap DB counts) — surfaced in the dashboard control card.
    coverage = None
    try:
        from . import autofill_engine as _aec
        coverage = int(_aec._plex_match_coverage_pct(1))
    except Exception:
        coverage = None
    try:
        import json as _jsonp
        from .db import get_config as _gcp
        _gaps_last = (_jsonp.loads(_gcp("requeue_scan_last_json") or "{}")).get("requeued")
    except Exception:
        _gaps_last = None
    return jsonify({
        "gaps_requeued_last": _gaps_last,
        "paused": paused,
        "next_run_in_seconds": next_in,
        "max_instances": max_inst,
        "tick_interval_seconds": tick_interval,
        "in_flight": in_flight,
        "queue_depth": queue_depth,
        "cooldown": cooldown,
        "streaming_paused": streaming_paused,
        "streaming_sessions": streaming_sessions,
        "concurrency_ceiling": conc_ceiling,
        "plex_coverage_pct": coverage,
    })


@bp.route("/api/picker/fill-balance", methods=["GET", "POST"])
def api_picker_fill_balance():
    """Fill balance control. POST saves mode (auto|manual) + slider value;
    GET returns the live state the popover renders: workload counts and what
    the engine is EFFECTIVELY doing right now."""
    from flask import jsonify
    if request.method == "POST":
        mode = (request.form.get("mode") or "").lower()
        if mode in ("auto", "manual"):
            set_config("autofill_fill_mode", mode)
        if "value" in request.form:
            try:
                v = max(0, min(4, int(request.form.get("value", "2"))))
                set_config("autofill_fill_balance", str(v))
            except Exception:
                pass
    from .db import AutofillAction
    from .autofill_engine import _effective_fill_per_4
    with session_scope() as s:
        queued = s.query(AutofillAction).filter(AutofillAction.status == "queued").count()
        filling = s.query(AutofillAction).filter(AutofillAction.status == "imported").count()
    fill4 = _effective_fill_per_4(queued)
    return jsonify({
        "ok": True,
        "mode": get_config("autofill_fill_mode", "auto"),
        "value": int(get_config("autofill_fill_balance", "2") or 2),
        "queued": queued, "filling": filling,
        "fill_per_4": fill4, "acquire_per_4": 4 - fill4,
    })


@bp.route("/api/picker/pause", methods=["POST"])
def api_picker_pause():
    from flask import jsonify
    try:
        from .main import scheduler
        scheduler.pause_job("library_autofill_picker")
        return jsonify({"ok": True, "paused": True})
    except Exception as e:
        log.exception("api_picker_pause failed")
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/picker/resume", methods=["POST"])
def api_picker_resume():
    from flask import jsonify
    import threading
    # Best-effort resume of the scheduled job (no-op in UI-only mode where the scheduler
    # isn't started) — but ALWAYS fire one tick immediately so "Resume picker" actually
    # acts (organizes staging + kicks off any import-folder drops) on every deployment.
    try:
        from .main import scheduler
        scheduler.resume_job("library_autofill_picker")
    except Exception:
        log.info("api_picker_resume: resume_job skipped (scheduler not running / job absent)")
    try:
        from .autofill_engine import picker_tick
        threading.Thread(target=picker_tick, name="picker-resume-tick", daemon=True).start()
    except Exception as e:
        log.exception("api_picker_resume: immediate tick failed")
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "paused": False})


@bp.route("/api/picker/run-now", methods=["POST"])
def api_picker_run_now():
    """Fire picker_tick immediately (in-thread); does not affect schedule."""
    from flask import jsonify
    import threading
    try:
        from .autofill_engine import picker_tick
        threading.Thread(target=picker_tick, name="picker-run-now", daemon=True).start()
        return jsonify({"ok": True, "fired": True})
    except Exception as e:
        log.exception("api_picker_run_now failed")
        return jsonify({"ok": False, "error": str(e)}), 500



@bp.route("/api/jobs/recent")
def api_jobs_recent():
    """Recent jobs as JSON — the native Jobs page's list (the /jobs HTML view uses
    jobs.recent() server-side; this exposes the same for the SwiftUI client)."""
    from flask import jsonify, request
    try:
        limit = max(1, min(100, int(request.args.get("limit", 50))))
    except (TypeError, ValueError):
        limit = 50
    return jsonify({"recent": jobs.recent(limit)})


@bp.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    """Read/write the config the native Settings page shows. GET masks secrets (only
    reports whether a token is set); POST applies the simple toggles from a JSON body."""
    from flask import jsonify, request
    import json as _json
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        if "autofill_enabled" in data:
            set_config("autofill_enabled", "1" if data.get("autofill_enabled") else "0")
        if "autofill_interval_minutes" in data:
            try:
                iv = max(5, min(360, int(data["autofill_interval_minutes"])))
                set_config("autofill_interval_minutes", str(iv))
                try:
                    from .main import scheduler as _sched
                    _sched.reschedule_job("library_autofill_picker", trigger="interval", minutes=iv)
                except Exception:
                    pass
            except Exception:
                pass
        if "hires_only" in data:
            set_config("autofill_soulseek_hires_only", "1" if data.get("hires_only") else "0")
        if "pause_when_streaming" in data:
            set_config("autofill_pause_when_streaming", "1" if data.get("pause_when_streaming") else "0")
        if "quality_target" in data:
            hires = str(data.get("quality_target") or "").upper().startswith("HI_RES")
            set_config("autofill_soulseek_hires_only", "1" if hires else "0")
        if "autostar_manage_enabled" in data:
            set_config("autostar_manage_enabled", "1" if data.get("autostar_manage_enabled") else "0")
        if "autostar_dry_run" in data:
            set_config("autostar_dry_run", "1" if data.get("autostar_dry_run") else "0")
        # --- Connections + sources config (mirrors the web Settings forms) ---
        # Secrets: a blank/absent value KEEPS the saved one; a non-empty value replaces it.
        for _sk in ("slskd_api_key", "anthropic_api_key", "telegram_api_hash", "telegram_session"):
            if _sk in data:
                _sv = str(data.get(_sk) or "").strip()
                if _sv:
                    set_config(_sk, _sv)
        if "spotiflac_qobuz_token" in data:   # supports explicit clear via "__clear__"
            _qv = str(data.get("spotiflac_qobuz_token") or "").strip()
            if _qv == "__clear__":
                set_config("spotiflac_qobuz_token", "")
            elif _qv:
                set_config("spotiflac_qobuz_token", _qv)
        # Plain text / url / number config.
        for _tk in ("plex_library_path", "slskd_url", "anthropic_model", "telegram_api_id",
                    "manual_import_path",
                    "audiobook_drop_path", "audiobook_library_path", "audiobook_intake_path",
                    "plex_audiobook_section_key", "audiobook_min_confidence"):
            if _tk in data:
                set_config(_tk, str(data.get(_tk) or "").strip())
        # Boolean toggles.
        for _bk in ("telegram_enabled", "smart_update_enabled", "self_repair_bypass",
                    "autofill_picker_enabled", "autofill_strict_flac",
                    "autofill_allow_mp3_fallback", "autofill_allow_cd_quality",
                    "manual_import_enabled", "manual_import_delete_unnecessary",
                    "manual_import_dry_run", "manual_import_require_liked",
                    "manual_import_songs_only", "audiobook_enabled"):
            if _bk in data:
                set_config(_bk, "1" if data.get(_bk) else "0")
        # Audiobook settings also live in the daemon's own config DB — forward on change.
        if any(k in data for k in ("audiobook_enabled", "audiobook_min_confidence")):
            try:
                from . import audiobook_client
                audiobook_client.push_config()
            except Exception:
                log.exception("audiobook config push failed (settings saved locally)")
        if "autofill_acquisition_mode" in data:
            _m = str(data.get("autofill_acquisition_mode") or "album")
            if _m in ("song", "album", "discography"):
                set_config("autofill_acquisition_mode", _m)
        # Catalog-source toggles live in the autofill_sources_json list (preserve playlist:* entries).
        if "source_liked" in data or "source_followed_artists" in data:
            try:
                _cur = set(_json.loads(get_config("autofill_sources_json") or "[]"))
            except Exception:
                _cur = set()
            if "source_liked" in data:
                _cur.discard("liked")
                if data.get("source_liked"):
                    _cur.add("liked")
            if "source_followed_artists" in data:
                _cur.discard("followed_artists")
                if data.get("source_followed_artists"):
                    _cur.add("followed_artists")
            set_config("autofill_sources_json", _json.dumps(sorted(_cur)))
        # Risky "gated" fields — only applied when Self-repair is unlocked (FULL or bypass),
        # mirroring the web UI's lock so a bad edit can't silently break a source.
        _full = (get_config("smart_update_enabled", "0") == "1") and bool(get_config("anthropic_api_key", ""))
        if _full or (get_config("self_repair_bypass", "0") == "1"):
            for _gk in ("squid_base", "spotiflac_repo", "telegram_bot"):
                if _gk in data:
                    set_config(_gk, str(data.get(_gk) or "").strip())
        return jsonify({"ok": True})

    def _b(key, default="0"):
        return (get_config(key, default) or default) == "1"
    try:
        sources = _json.loads(get_config("autofill_sources_json") or "[]")
    except Exception:
        sources = []
    try:
        prio = _json.loads(get_config("autofill_source_priority_json") or "[]")
    except Exception:
        prio = []
    try:
        interval = int(get_config("autofill_interval_minutes", "30") or 30)
    except Exception:
        interval = 30
    hires = _b("autofill_soulseek_hires_only")
    tg_enabled = tg_cfg = False
    try:
        from . import telegram_picker as _tg
        tc = _tg._cfg()
        tg_enabled = bool(tc.get("enabled"))
        tg_cfg = bool(tc.get("api_id") and tc.get("api_hash") and tc.get("session"))
    except Exception:
        pass
    squid_enabled = False
    try:
        from . import squid_adapter as _sq
        squid_enabled = bool(_sq.is_enabled())
    except Exception:
        pass
    try:
        spotify_authed = bool(auth_spotify.is_authed())
    except Exception:
        spotify_authed = False
    return jsonify({
        "autofill_enabled": _b("autofill_enabled"),
        "autofill_interval_minutes": interval,
        "quality_target": "HI_RES_LOSSLESS" if hires else "LOSSLESS",
        "quality_profile_name": get_config("autofill_quality_profile_name", "") or "",
        "sources": sources,
        "source_priority": prio,
        "hires_only": hires,
        "pause_when_streaming": _b("autofill_pause_when_streaming", "1"),
        "fill_mode": get_config("autofill_fill_mode", "auto"),
        "fill_balance": int(get_config("autofill_fill_balance", "2") or 2),
        "slskd_url": get_config("slskd_url", "") or "",
        "lidarr_url": get_config("lidarr_url", "") or "",
        "plex_url": get_config("plex_url", "") or "",
        "plex_token_set": bool(get_config("plex_token", "")),
        "telegram_enabled": tg_enabled,
        "telegram_configured": tg_cfg,
        "squid_enabled": squid_enabled,
        "spotify_authed": spotify_authed,
        "nas_downloader_url": get_config("nas_downloader_url", "") or "",
        "autostar_manage_enabled": _b("autostar_manage_enabled"),
        "autostar_dry_run": _b("autostar_dry_run"),
        "ownership_attested": _b("ownership_attested"),
        # Connections (secrets masked → only whether they're set)
        "plex_library_path": get_config("plex_library_path", "/plexify-music") or "/plexify-music",
        "slskd_api_key_set": bool(get_config("slskd_api_key", "")),
        "squid_base": get_config("squid_base", "https://qobuz.squid.wtf") or "",
        "spotiflac_repo": get_config("spotiflac_repo", "ShuShuzinhuu/SpotiFLAC-Module-Version") or "",
        "spotiflac_qobuz_token_set": bool(get_config("spotiflac_qobuz_token", "")),
        "telegram_api_id": get_config("telegram_api_id", "") or "",
        "telegram_api_hash_set": bool(get_config("telegram_api_hash", "")),
        "telegram_session_set": bool(get_config("telegram_session", "")),
        "telegram_bot": get_config("telegram_bot", "@BeatSpotBot") or "",
        # Self-repair
        "self_repair_bypass": _b("self_repair_bypass"),
        "smart_update_enabled": _b("smart_update_enabled"),
        "anthropic_api_key_set": bool(get_config("anthropic_api_key", "")),
        "anthropic_model": get_config("anthropic_model", "claude-opus-4-8") or "",
        "self_repair_full": _b("smart_update_enabled") and bool(get_config("anthropic_api_key", "")),
        # Downloading
        "autofill_picker_enabled": _b("autofill_picker_enabled"),
        "autofill_acquisition_mode": get_config("autofill_acquisition_mode", "album") or "album",
        "autofill_strict_flac": _b("autofill_strict_flac", "1"),
        "autofill_allow_mp3_fallback": _b("autofill_allow_mp3_fallback"),
        "autofill_allow_cd_quality": _b("autofill_allow_cd_quality"),
        "source_liked": ("liked" in sources),
        "source_followed_artists": ("followed_artists" in sources),
        # Appearance
        "liked_songs_cover": get_config("liked_songs_cover", "star") or "star",
        # Manual import
        "manual_import_enabled": _b("manual_import_enabled"),
        "manual_import_path": get_config("manual_import_path",
                                         "/Volumes/MediaVolume3/plexify-imports") or "",
        "manual_import_delete_unnecessary": _b("manual_import_delete_unnecessary"),
        "manual_import_dry_run": _b("manual_import_dry_run"),
        "manual_import_require_liked": _b("manual_import_require_liked"),
        "manual_import_songs_only": _b("manual_import_songs_only"),
        # Audiobooks
        "audiobook_enabled": _b("audiobook_enabled"),
        "audiobook_drop_path": get_config(
            "audiobook_drop_path", "/Volumes/MediaVolume3/plexify-imports") or "",
        "audiobook_intake_path": get_config(
            "audiobook_intake_path",
            "/Volumes/MediaVolume3/Downloads/audiobooks/auto-m4b/recentlyadded") or "",
        "audiobook_library_path": get_config(
            "audiobook_library_path", "/Volumes/MediaVolume3/Audiobooks") or "",
        "plex_audiobook_section_key": get_config("plex_audiobook_section_key", "") or "",
        "audiobook_min_confidence": get_config("audiobook_min_confidence", "80") or "80",
    })


@bp.route("/api/unmatched/suggestions")
def api_unmatched_suggestions():
    """Ranked 'acquire this artist → +coverage' suggestions (the smart suggestor)."""
    from flask import jsonify, request
    from . import suggestor
    try:
        min_tracks = max(1, int(request.args.get("min_tracks",
                                                  get_config("suggestor_min_tracks", "3") or 3)))
    except (TypeError, ValueError):
        min_tracks = 3
    try:
        limit = max(1, min(100, int(request.args.get("limit", 25))))
    except (TypeError, ValueError):
        limit = 25
    try:
        return jsonify(suggestor.coverage_gap_suggestions(min_tracks=min_tracks, limit=limit))
    except Exception as e:
        log.exception("suggestions failed")
        return jsonify({"suggestions": [], "current_coverage_pct": 0, "wanted_total": 0,
                        "covered_total": 0, "error": str(e)[:200]})


@bp.route("/api/unmatched/suggestions/dismiss", methods=["POST"])
def api_unmatched_suggestions_dismiss():
    from flask import jsonify, request
    from . import suggestor
    ak = request.args.get("artist") or (request.get_json(silent=True) or {}).get("artist") or ""
    return jsonify(suggestor.dismiss_artist(ak))


@bp.route("/api/unmatched/suggestions/requeue", methods=["POST"])
def api_unmatched_suggestions_requeue():
    from flask import jsonify, request
    from . import suggestor
    ak = request.args.get("artist") or (request.get_json(silent=True) or {}).get("artist") or ""
    try:
        return jsonify(suggestor.requeue_artist(ak))
    except Exception as e:
        log.exception("requeue failed")
        return jsonify({"ok": False, "error": str(e)[:200]})


@bp.route("/manual-import/preview", methods=["POST"])
def manual_import_preview():
    """Start a dry-run in the BACKGROUND (classify everything, move/delete NOTHING). A big drop
    exceeds the HTTP timeout, so this returns immediately; poll /manual-import/status for the tally."""
    from flask import jsonify
    from . import import_folder
    started = import_folder.start_scan_async(dry_run=True)
    return jsonify({"ok": True, "started": started,
                    "error": None if started else "a scan is already running"})


@bp.route("/manual-import/scan", methods=["POST"])
def manual_import_scan_route():
    """Start the real import in the BACKGROUND: sort keepers into the library, discard the rest.
    Returns immediately; poll /manual-import/status for the result."""
    from flask import jsonify
    from . import import_folder
    if not import_folder.manual_import_enabled():
        return jsonify({"ok": False, "error": "manual import is off — enable it first"})
    started = import_folder.start_scan_async(dry_run=False)
    return jsonify({"ok": True, "started": started,
                    "error": None if started else "a scan is already running"})


@bp.route("/manual-import/status")
def manual_import_status():
    """Poll target for the async preview/scan — {running, kind, at, result}."""
    from flask import jsonify
    from . import import_folder
    return jsonify(import_folder.scan_status())


@bp.route("/api/unmatched")
def api_unmatched():
    """Unmatched-tracks log as JSON (the native Unmatched page)."""
    from flask import jsonify, request
    try:
        limit = max(1, min(500, int(request.args.get("limit", 200))))
    except (TypeError, ValueError):
        limit = 200
    rows_out = []
    with session_scope() as s:
        total = s.query(UnmatchedTrack).count()
        rows = list(s.scalars(
            select(UnmatchedTrack).order_by(desc(UnmatchedTrack.last_seen_at)).limit(limit)
        ).all())
        for r in rows:
            rows_out.append({
                "id": r.id, "artist": r.artist, "title": r.title, "album": r.album,
                "reason": r.reason,
                "last_seen_at": (r.last_seen_at.isoformat() + "Z") if r.last_seen_at else None,
            })
    return jsonify({"rows": rows_out, "total": total, "shown": len(rows_out), "limit": limit})


@bp.route("/api/playlists")
def api_playlists():
    """Playlist mirror-pairs as JSON (the native Playlists page). Cheap DB-only view —
    no Spotify/Plex API calls, so it's safe to poll."""
    from flask import jsonify
    from .db import LocalTrack
    items = []
    with session_scope() as s:
        pairs = list(s.scalars(select(PlaylistPair)).all())
        for p in pairs:
            try:
                tc = s.query(LocalTrack).filter(LocalTrack.pair_id == p.id).count()
            except Exception:
                tc = 0
            synced = p.compile_completed_at or p.last_changed_at
            items.append({
                "id": p.id, "name": p.name, "spotify_name": p.name,
                "spotify_id": p.spotify_playlist_id,
                "track_count": tc or (p.compile_total or 0),
                "mirrored": bool(p.plex_enabled and p.plex_playlist_key),
                "last_synced_at": (synced.isoformat() + "Z") if synced else None,
                "status": p.compile_status,
            })
    src = None
    try:
        if auth_spotify.is_authed():
            src = "Spotify"
    except Exception:
        pass
    return jsonify({"items": items, "source_name": src, "total": len(items)})


_NAS_CACHE = {"reachable": True, "queued": 0, "running": 0, "ready": 0, "failed": 0,
              "jobs": [], "ts": 0.0}   # daemon status, refreshed off the request path
_NAS_REFRESHING = [False]
_NAS_FAILS = [0]                        # consecutive healthz failures (smooths transient timeouts)


def _refresh_nas_daemon_status():
    """Background refresh of the daemon's /healthz + /queue. Runs OFF the request path so the
    (Tailscale) daemon calls — which crawl when a big import saturates the WAN link — never hang
    the dashboard poll. reachable only flips to False after 3 consecutive failures, so a single
    timeout under import load doesn't flap the pill to 'unreachable' (found 2026-07-07)."""
    import time as _t
    from . import nas_downloader as _nd
    try:
        h = _nd._req("/healthz", timeout=3)
        _NAS_CACHE["reachable"] = bool(h.get("ok", True))
        for k in ("queued", "running", "ready", "failed"):
            _NAS_CACHE[k] = int(h.get(k, 0) or 0)
        _NAS_FAILS[0] = 0
        try:
            q = _nd._req("/queue", timeout=4)
            run = [j for j in (q.get("jobs") or []) if j.get("status") == "running"]
            _NAS_CACHE["jobs"] = [{"artist": j.get("artist"), "album": j.get("album"),
                                   "title": j.get("title"), "source": j.get("source")}
                                  for j in run[:8]]
        except Exception:
            pass
    except Exception:
        _NAS_FAILS[0] += 1
        if _NAS_FAILS[0] >= 3:
            _NAS_CACHE["reachable"] = False
    finally:
        _NAS_CACHE["ts"] = _t.time()
        _NAS_REFRESHING[0] = False


@bp.route("/api/nas-downloader/status")
def api_nas_downloader_status():
    """Status of the autonomous NAS downloader daemon (:8788). FULLY NON-BLOCKING: serves a
    cached daemon status refreshed in the background + the staging count (a scan's live countdown,
    else a cached folder walk). Never does a synchronous SMB/Tailscale call on the request path,
    because while a big import saturates the WAN link ANY such call hangs the poll — which used to
    time out the endpoint and draw the NAS pill as 'unreachable' even though the daemon was up."""
    import time as _t, threading as _th
    from flask import jsonify, request
    _fresh = request.args.get("fresh") == "1"
    # staging count — instant (scan countdown, or cached; force does a bounded recount)
    try:
        from . import import_folder
        import_pending = import_folder.pending_count(force=_fresh)
    except Exception:
        import_pending = 0
    # daemon status — serve cached, kick a background refresh when stale
    if (_fresh or _t.time() - _NAS_CACHE["ts"] >= 4) and not _NAS_REFRESHING[0]:
        _NAS_REFRESHING[0] = True
        _th.Thread(target=_refresh_nas_daemon_status, daemon=True, name="nas-status").start()
    out = {k: _NAS_CACHE[k] for k in ("reachable", "queued", "running", "ready", "failed", "jobs")}
    out["import_pending"] = import_pending
    return jsonify(out)


# ── audiobooks ────────────────────────────────────────────────────────────────
_AB_CACHE = {"reachable": False, "enabled": False, "dirs_ok": False, "dropped": 0,
             "imports_waiting": 0, "converting": 0, "untagged": 0, "review": 0,
             "review_items": [], "converter": {}, "working_on": None,
             "organized_total": 0, "recent": [], "offline": False,
             "library_visible": False, "ts": 0.0}
_AB_REFRESHING = [False]


def _refresh_audiobook_status():
    """Background refresh of the daemon-side organizer status. ALSO runs audiobook_tick (config
    push + Plex-scan-on-new-books) — trigger-on-poll, so the feature works on deployments where
    the engine scheduler is off (the dashboard/page poll IS the tick)."""
    import time as _t
    try:
        from . import audiobook_client
        audiobook_client.audiobook_tick()
        st = audiobook_client.daemon_status()
        for k in ("reachable", "enabled", "dirs_ok", "dropped", "imports_waiting", "converting",
                  "untagged", "review", "review_items", "converter", "working_on", "offline",
                  "organized_total", "recent", "library_visible"):
            if k in st:
                _AB_CACHE[k] = st[k]
        if not st.get("reachable"):
            _AB_CACHE["reachable"] = False
    except Exception:
        log.exception("audiobook status refresh failed")
    finally:
        _AB_CACHE["ts"] = _t.time()
        _AB_REFRESHING[0] = False


@bp.route("/api/audiobooks/status")
def api_audiobooks_status():
    """Organizer pipeline status for the Audiobooks page. Non-blocking: cached, refreshed off
    the request path (the refresh also runs the engine-side tick — see above)."""
    import time as _t, threading as _th
    from flask import jsonify
    if _t.time() - _AB_CACHE["ts"] >= 4 and not _AB_REFRESHING[0]:
        _AB_REFRESHING[0] = True
        _th.Thread(target=_refresh_audiobook_status, daemon=True, name="ab-status").start()
    out = {k: v for k, v in _AB_CACHE.items() if k != "ts"}
    out["feature_enabled"] = (get_config("audiobook_enabled", "0") or "0") == "1"
    return jsonify(out)


@bp.route("/api/audiobooks/organize-now", methods=["POST"])
def api_audiobooks_organize_now():
    from flask import jsonify
    from . import audiobook_client
    audiobook_client.push_config()
    res = audiobook_client.organize_now()
    # human message for the button — 'Organize now' used to give no feedback at all
    if not res.get("ok"):
        res["message"] = res.get("error") or "couldn't reach the organizer daemon"
    elif not res.get("started"):
        res["message"] = res.get("error") or "a pass is already running"
    else:
        n = int(res.get("untagged") or 0) + int(res.get("imports_waiting") or 0)
        res["message"] = (f"organizing — {n} waiting" if n > 0
                          else "started — nothing waiting right now")
    _AB_CACHE["ts"] = 0.0
    return jsonify(res)


@bp.route("/api/audiobooks/resolve", methods=["POST"])
def api_audiobooks_resolve():
    from flask import jsonify, request
    from . import audiobook_client
    data = request.get_json(force=True, silent=True) or {}
    res = audiobook_client.resolve(str(data.get("file") or ""),
                                   asin=data.get("asin") or None,
                                   author=data.get("author") or None,
                                   title=data.get("title") or None)
    if res.get("ok"):
        # a resolve files a book directly — make Plex pick it up right away
        try:
            from . import plex_client
            plex_client.trigger_audiobook_scan()
        except Exception:
            pass
    return jsonify(res)


_AB_LIB_CACHE = {"items": [], "ts": 0.0}
_AB_LIB_REFRESHING = [False]


@bp.route("/api/audiobooks/library")
def api_audiobooks_library():
    """All books in the Plex audiobook section (title/author/rel_dir) — feeds the app's
    manage-library list. Cached + refreshed off the request path like the status endpoint."""
    import time as _t, threading as _th
    from flask import jsonify

    def _refresh():
        try:
            from . import plex_client
            _AB_LIB_CACHE["items"] = plex_client.list_audiobook_albums()
        except Exception:
            log.exception("audiobook library refresh failed")
        finally:
            _AB_LIB_CACHE["ts"] = _t.time()
            _AB_LIB_REFRESHING[0] = False

    if _t.time() - _AB_LIB_CACHE["ts"] >= 30 and not _AB_LIB_REFRESHING[0]:
        _AB_LIB_REFRESHING[0] = True
        _th.Thread(target=_refresh, daemon=True, name="ab-library").start()
    return jsonify({"items": _AB_LIB_CACHE["items"]})


@bp.route("/api/audiobooks/cover/<int:key>")
def api_audiobooks_cover(key):
    """Album art for the shelf grid, proxied from Plex (token stays server-side)."""
    from flask import Response
    from . import plex_client
    data = plex_client.audiobook_cover(key)
    if not data:
        return Response(status=404)
    return Response(data, mimetype="image/jpeg",
                    headers={"Cache-Control": "max-age=3600"})


@bp.route("/api/audiobooks/delete", methods=["POST"])
def api_audiobooks_delete():
    """Soft-delete a book: the daemon moves its folder to trash/ (NEVER unlinks), Plex entry
    cleaned up in the background. The app confirms with the user before calling."""
    from flask import jsonify, request
    from . import audiobook_client
    data = request.get_json(force=True, silent=True) or {}
    res = audiobook_client.delete_book(rel_dir=str(data.get("rel_dir") or ""),
                                       dest=str(data.get("dest") or ""))
    if res.get("ok"):
        _AB_LIB_CACHE["ts"] = 0.0        # the shelf changed — next poll re-lists
        _AB_CACHE["ts"] = 0.0
    return jsonify(res)


@bp.route("/api/audiobooks/discard", methods=["POST"])
def api_audiobooks_discard():
    """Discard a review-parked file to trash/ — for drops the user doesn't want resolved."""
    from flask import jsonify, request
    from . import audiobook_client
    data = request.get_json(force=True, silent=True) or {}
    res = audiobook_client.discard_review(str(data.get("file") or ""))
    if res.get("ok"):
        _AB_CACHE["ts"] = 0.0
    return jsonify(res)


@bp.route("/api/audiobooks/search")
def api_audiobooks_search():
    """Assistive/type-ahead book search — Audible catalog, returned FAST (no Soulseek gate).
    The UI fires /availability next to badge each result."""
    from flask import jsonify, request
    from . import audiobook_client
    return jsonify(audiobook_client.search_books(request.args.get("q", "")))


@bp.route("/api/audiobooks/availability", methods=["POST"])
def api_audiobooks_availability():
    """Soulseek availability for a batch of search results — {asin: bool}. Slow (~15s: probes
    slskd per item), so the UI calls it AFTER showing results and badges them when it returns."""
    from flask import jsonify, request
    from . import audiobook_client
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(audiobook_client.availability(data.get("items") or []))


@bp.route("/api/audiobooks/wanted")
def api_audiobooks_wanted():
    from flask import jsonify
    from . import audiobook_client
    return jsonify(audiobook_client.wanted())


@bp.route("/api/audiobooks/want", methods=["POST"])
def api_audiobooks_want():
    from flask import jsonify, request
    from . import audiobook_client
    data = request.get_json(force=True, silent=True) or {}
    res = audiobook_client.want({k: data.get(k) for k in
                                 ("asin", "title", "author", "runtime_min", "reason")})
    return jsonify(res)


@bp.route("/api/audiobooks/unwant", methods=["POST"])
def api_audiobooks_unwant():
    from flask import jsonify, request
    from . import audiobook_client
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(audiobook_client.unwant(str(data.get("asin") or ""),
                                           str(data.get("title") or "")))


@bp.route("/api/audiobooks/dismiss", methods=["POST"])
def api_audiobooks_dismiss():
    from flask import jsonify, request
    from . import audiobook_client
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(audiobook_client.dismiss_suggestion(str(data.get("asin") or "")))


@bp.route("/api/plex/audiobook-sections")
def api_plex_audiobook_sections():
    from flask import jsonify
    from . import plex_client
    try:
        return jsonify({"sections": plex_client.list_audiobook_sections(),
                        "selected": get_config("plex_audiobook_section_key", "") or ""})
    except Exception as e:
        return jsonify({"sections": [], "error": str(e)[:200]})


@bp.route("/api/audiobooks/create-plex-section", methods=["POST"])
def api_audiobooks_create_plex_section():
    from flask import jsonify, request
    from . import plex_client
    data = request.get_json(force=True, silent=True) or {}
    location = str(data.get("location") or "/media/vol3/Audiobooks")
    res = plex_client.create_audiobook_section(location)
    if res.get("ok") and res.get("key"):
        set_config("plex_audiobook_section_key", str(res["key"]))
    return jsonify(res)


@bp.route("/api/attest", methods=["POST"])
def api_attest():
    """Record the legal-use attestation (the 'I'll use this legally' agreement). This is
    what unlocks downloading — the authoritative gate is in nas_downloader.enqueue_and_wait."""
    from flask import jsonify
    from datetime import datetime as _dt
    set_config("ownership_attested", "1")
    set_config("ownership_attested_at", _dt.utcnow().isoformat() + "Z")
    return jsonify({"ok": True})


@bp.route("/api/attest/status")
def api_attest_status():
    """Poll target for the native app + web to learn whether the legal gate is satisfied."""
    from flask import jsonify
    attested = (get_config("ownership_attested", "0") or "0") == "1"
    return jsonify({"attested": attested,
                    "attested_at": (get_config("ownership_attested_at", "") or None)})


@bp.route("/api/diagnostics/spotiflac", methods=["GET"])
def api_diagnostics_spotiflac():
    """Report installed SpotiFLAC version + check for newer commits on upstream."""
    from flask import jsonify
    import subprocess, json as _json, urllib.request, datetime as _dt_d
    out = {"installed_version": None, "installed_commit": None,
           "upstream_main_commit": None, "upstream_main_date": None,
           "up_to_date": None, "checked_at": _dt_d.datetime.utcnow().isoformat() + "Z"}
    # Installed version
    try:
        r = subprocess.run(["pip", "show", "SpotiFLAC"], capture_output=True, text=True, timeout=10)
        for line in r.stdout.splitlines():
            if line.startswith("Version:"):
                out["installed_version"] = line.split(":", 1)[1].strip()
    except Exception:
        pass
    # Upstream HEAD on main
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/%s/branches/main" % (get_config("spotiflac_repo", "ShuShuzinhuu/SpotiFLAC-Module-Version") or "ShuShuzinhuu/SpotiFLAC-Module-Version"),
            headers={"User-Agent": "plexify-diagnostics"},
        )
        resp = urllib.request.urlopen(req, timeout=8).read().decode()
        d = _json.loads(resp)
        out["upstream_main_commit"] = d["commit"]["sha"][:10]
        out["upstream_main_date"] = d["commit"]["commit"]["author"]["date"]
    except Exception as e:
        out["upstream_check_error"] = str(e)[:120]
    # Populate installed_commit via the autofill_engine helper.
    try:
        from .autofill_engine import _get_installed_spotiflac_commit
        ic = _get_installed_spotiflac_commit()
        out["installed_commit"] = ic
        if ic and out.get("upstream_main_commit"):
            out["up_to_date"] = ic.startswith(out["upstream_main_commit"][:10])
    except Exception:
        pass
    # Also include hourly success counts (drives the adaptive update)
    try:
        from .autofill_engine import get_provider_success_counts_1h
        out["success_counts_1h"] = get_provider_success_counts_1h()
    except Exception:
        pass
    return jsonify(out)



@bp.route("/settings/update-spotiflac", methods=["POST"])
def update_spotiflac():
    """In-container pip-upgrade SpotiFLAC from git+main, then trigger restart.

    Note: container restart is asynchronous — we send SIGTERM to PID 1 (tini)
    AFTER returning the response. Docker auto-restarts the container (because
    `restart: unless-stopped`), which gives clean process-state and fresh code.
    In-flight picks are aborted; watchdog cleans them up.
    """
    from flask import jsonify
    import subprocess, threading, os, signal, time
    out = {"ok": False}
    try:
        # Current version
        _curr = subprocess.run(["pip", "show", "SpotiFLAC"], capture_output=True, text=True, timeout=10)
        out["old_version"] = next(
            (l.split(":", 1)[1].strip() for l in _curr.stdout.splitlines() if l.startswith("Version:")),
            "?",
        )
        # Upgrade from git+main, force-reinstall to ensure pip picks up new commits
        # even if the version number didn't change.
        result = subprocess.run(
            [
                "pip", "install", "--upgrade", "--no-deps", "--force-reinstall",
                "git+https://github.com/ShuShuzinhuu/SpotiFLAC-Module-Version.git@main",
            ],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0:
            out["error"] = (result.stderr or result.stdout)[-500:]
            return jsonify(out), 500
        # Self-heal SpotiFLAC-main's recurring 'NameError: Optional' before restarting.
        try:
            from . import autofill_engine as _ae_p
            out["patched_modules"] = _ae_p.patch_spotiflac_future_annotations()
        except Exception:
            log.exception("update-spotiflac: post-install patch failed")
        # If it still doesn't import, let Claude (Opus) smart-repair it (when configured).
        try:
            _vimp = subprocess.run(["python", "-c", "import SpotiFLAC"], capture_output=True, text=True, timeout=40)
            if _vimp.returncode != 0:
                from . import smart_update as _su
                if _su.is_configured():
                    out["smart_update"] = _su.llm_repair(_vimp.stderr or "")
        except Exception:
            log.exception("update-spotiflac: smart-repair attempt failed")
        _new = subprocess.run(["pip", "show", "SpotiFLAC"], capture_output=True, text=True, timeout=10)
        out["new_version"] = next(
            (l.split(":", 1)[1].strip() for l in _new.stdout.splitlines() if l.startswith("Version:")),
            "?",
        )
        out["stdout_tail"] = result.stdout[-300:]
        out["ok"] = True
        out["restart_pending"] = True

        # Schedule restart AFTER response returns. Docker's restart policy will
        # bring the container back up automatically with fresh imports.
        def _restart_later():
            time.sleep(2)
            try:
                log.info("update-spotiflac: sending SIGTERM to PID 1 for graceful restart")
                os.kill(1, signal.SIGTERM)
            except Exception:
                log.exception("update-spotiflac: failed to signal restart")
        threading.Thread(target=_restart_later, name="spotiflac-restart", daemon=True).start()

        return jsonify(out)
    except subprocess.TimeoutExpired:
        out["error"] = "pip install timed out after 180s (network or git slow?)"
        return jsonify(out), 500
    except Exception as e:
        log.exception("update-spotiflac: unexpected failure")
        out["error"] = str(e)
        return jsonify(out), 500



@bp.route("/library-autofill/save-qobuz-token", methods=["POST"])
def library_autofill_save_qobuz_token():
    """Save the optional Qobuz auth token used by SpotiFLAC for authenticated
    Tidal+Qobuz downloads. Empty string clears the token."""
    from flask import jsonify
    raw = (request.form.get("spotiflac_qobuz_token") or "").strip()
    # Light sanity: tokens are typically long opaque strings — reject obviously short ones
    # but allow empty (clear).
    if raw and len(raw) < 8:
        is_fetch = request.headers.get("X-Requested-With") == "fetch"
        if is_fetch:
            return jsonify({"ok": False, "error": "token looks too short (min 8 chars)"}), 400
        flash("Qobuz token looks too short — not saved.", "error")
        return redirect(url_for("main.library_autofill"))
    set_config("spotiflac_qobuz_token", raw)
    is_fetch = request.headers.get("X-Requested-With") == "fetch"
    if is_fetch:
        return jsonify({
            "ok": True,
            "set": bool(raw),
            "preview": (raw[:4] + "…" + raw[-4:]) if raw and len(raw) > 12 else ("hidden" if raw else ""),
        })
    flash("Qobuz token " + ("saved." if raw else "cleared."), "success")
    return redirect(url_for("main.library_autofill"))
