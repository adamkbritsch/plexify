"""Application entrypoint: builds the Flask app and starts the background scheduler."""

import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask

from .config import SECRET_KEY, SYNC_INTERVAL_MINUTES
from .db import init_db
from .routes import bp
from .sync_engine import watch_additions, watch_with_deletions, trigger_immediate_watcher
from .autofill_engine import autofill_tick, reconcile_tick, picker_tick, discography_refresh_tick, sync_spotify_liked_tracks_tick, ensure_liked_songs_pair, upgrade_quality_tick, acquisition_watchdog_tick, spotiflac_health_check_tick, star_liked_songs_in_plex_tick, complete_albums_tick, mb_enrich_tick, sync_plex_ratings_to_spotify_tick, mark_present_queued_tick, requeue_missing_tick, autostar_place_tick, autostar_unstar_detect_tick
from .spotify_catalog import spotify_catalog_sync_tick
from .sync_engine import recompile_all_plex_playlists
from .playlist_sync import sync_all_playlists_bidirectional
from .sync_engine import smart_playlist_recompile_tick

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = SECRET_KEY
app.register_blueprint(bp)

init_db()

# Self-heal SpotiFLAC-main's recurring 'NameError: Optional' regression at startup,
# BEFORE anything imports SpotiFLAC. Patches the installed copy and the auto-update's
# user-site copy at runtime (there is no build-time patch step; the daemon patches
# itself the same way in downloader_daemon.main()).
try:
    from .autofill_engine import patch_spotiflac_future_annotations as _patch_sf
    _patch_sf()
except Exception:
    logging.getLogger("app.main").exception("startup SpotiFLAC patch failed (continuing)")

# Reset any zombie 'downloading' AutofillAction rows from a previous container
# crash (status='downloading' but no live picker is working on them).
def _reset_zombie_downloading_rows():
    try:
        from .db import session_scope, AutofillAction
        from sqlalchemy import update
        with session_scope() as s:
            n = s.execute(
                update(AutofillAction)
                .where(AutofillAction.status == "downloading")
                .values(status="queued")
            ).rowcount
        if n:
            logging.getLogger("app.main").info("zombie cleanup: reset %d rows from downloading -> queued", n)
    except Exception:
        logging.getLogger("app.main").exception("zombie cleanup failed (continuing)")

_reset_zombie_downloading_rows()
ensure_liked_songs_pair()

DELETIONS_INTERVAL_MINUTES = 60  # slow reconciliation pass

scheduler = BackgroundScheduler(daemon=True, timezone="UTC")
# Tiny watcher: 1 metadata call per tick, only re-syncs playlists whose snapshot_id changed.
scheduler.add_job(
    watch_additions, "interval", minutes=SYNC_INTERVAL_MINUTES,
    id="watch_additions", max_instances=1, coalesce=True,
)
# Slow watcher: same path + propagates deletions.
scheduler.add_job(
    watch_with_deletions, "interval", minutes=DELETIONS_INTERVAL_MINUTES,
    id="watch_deletions", max_instances=1, coalesce=True,
)
# Library autofill: scans Spotify sources, pushes missing albums to Lidarr.
# Interval is dynamic via AppConfig.autofill_interval_minutes (read inside the job).
try:
    from .db import get_config as _get_config
    _autofill_interval = int(_get_config("autofill_interval_minutes", "30") or 30)
except Exception:
    _autofill_interval = 30
scheduler.add_job(
    autofill_tick, "interval", minutes=_autofill_interval,
    id="library_autofill", max_instances=1, coalesce=True,
)
# B7: reconciler — promote queued AutofillActions to downloading/imported
# based on Lidarr queue + album file state. Fixed 10-min cadence.
scheduler.add_job(
    reconcile_tick, "interval", minutes=10,
    id="library_autofill_reconcile", max_instances=1, coalesce=True,
)
# Smart Soulseek picker — bypasses Lidarr's search/grab path. Default OFF;
# enable via /settings → autofill_picker_enabled. Serialized.
# Concurrency is AUTO-TUNED (AIMD) at runtime by the watchdog — no manual slots knob.
# We just seed the scheduler with the smart starting value + a matching interval.
# Capped at 2: picker_tick holds its DB transaction open during the whole move (ffmpeg integrity
# + file moves + ledger writes are INSIDE the session), so >2 concurrent ticks contend on SQLite's
# single write lock and the post-download status write aborts with "database is locked" — which
# silently re-queues the song into an endless re-download loop. 2 concurrent + busy_timeout=60s
# lets the writes serialize cleanly. (AIMD auto-tune disabled here on purpose.)
_picker_max = 2
_picker_interval = 8
scheduler.add_job(
    picker_tick, "interval", seconds=_picker_interval,
    id="library_autofill_picker", max_instances=_picker_max, coalesce=False,
)
logging.info("picker_tick scheduled: max_instances=%d interval=%ds", _picker_max, _picker_interval)
# Hi-res upgrade sweeper — re-acquires LOSSLESS imports at HI_RES_LOSSLESS.
# Polite: only runs when queue depth == 0 AND no in-flight acquisitions.
try:
    from .autofill_engine import upgrade_quality_tick
    scheduler.add_job(
        upgrade_quality_tick, "interval", hours=6,
        id="library_autofill_hires_upgrade", max_instances=1, coalesce=True,
    )
except Exception:
    logging.exception("could not register upgrade_quality_tick")

# MUSICBRAINZ ENRICHMENT — the authoritative 'mdb'. Gently caches each album's
# canonical album artist (incl. 'Various Artists' for soundtracks/compilations) so
# the picker places + tags from a real metadata source instead of inferring it.
# Only live MB caller; ~1 req/sec inside the client; small batch every 3 min.
scheduler.add_job(
    mb_enrich_tick, "interval", minutes=3,
    id="mb_enrich", max_instances=1, coalesce=True, misfire_grace_time=120,
)

# SMART PLAYLIST AUTO-RECOMPILE — when a new song lands in a sorted (non-source)
# playlist, re-apply its sort order. Smart: only re-sorts playlists whose track set
# actually changed + has settled, decoupled + debounced so it never thrashes. Every 6 min.
scheduler.add_job(
    smart_playlist_recompile_tick, "interval", minutes=6,
    id="smart_playlist_recompile", max_instances=1, coalesce=True, misfire_grace_time=120,
)

# DE-DUP / SKIP-PRESENT — mark queued rows whose album is already on disk (downloaded
# under another artist-credit or an earlier run) as 'library_existing', so the picker
# never needlessly re-attempts songs we already have. Every 5 min.
scheduler.add_job(
    mark_present_queued_tick, "interval", minutes=5,
    id="mark_present_queued", max_instances=1, coalesce=True, misfire_grace_time=120,
)

# REVERSE of mark_present_queued: re-queue 'have it' rows whose songs are genuinely NOT
# on disk (false 'already have' markers, atticed/removed files). Self-healing rotating
# disk-truth rescan so coverage corrects itself — no manual button needed.
scheduler.add_job(
    requeue_missing_tick, "interval", minutes=30,
    id="requeue_missing", max_instances=1, coalesce=True, misfire_grace_time=300,
)

# BIDIRECTIONAL PLAYLIST SYNC — add/remove a song on either Spotify or Plex and it
# mirrors to the other (3-way merge against a per-pair baseline). Spotify-only songs
# get queued for download. Runs every 10 min; the nightly recompile handles ordering.
scheduler.add_job(
    sync_all_playlists_bidirectional, "interval", minutes=10,
    id="playlist_bidirectional_sync", max_instances=1, coalesce=True, misfire_grace_time=120,
)

# PLEX RATING -> SPOTIFY LIKE — bidirectional companion to the star-push. When you
# 5★ a song in Plex/Plexamp it gets added to your Spotify Liked Songs. Only pushes
# ratings that aren't already liked. (Unstar reconcile is on-demand from Settings,
# not scheduled — the user wants the star-push automatic, not the unstar.)
scheduler.add_job(
    sync_plex_ratings_to_spotify_tick, "interval", minutes=15,
    id="plex_ratings_to_spotify", max_instances=1, coalesce=True, misfire_grace_time=120,
)

# PLEX UNDERSTANDING CHECK — after imports land, verify which artist Plex
# actually filed each new album under (it all depends on how Plex understands
# it — we cater to Plex, not the other way around). Repairs artist sort-title
# chimeras and re-homes mis-filed albums via the two-phase "Plex dance".
from .autofill_engine import plex_import_verify_tick
from datetime import datetime as _dt_piv, timedelta as _td_piv
scheduler.add_job(
    plex_import_verify_tick, "interval", minutes=20,
    id="plex_import_verify", max_instances=1, coalesce=True, misfire_grace_time=300,
    next_run_time=_dt_piv.now() + _td_piv(seconds=120),  # rebuilds must not starve it
)

# SHADOW-ALBUM cleanup — re-homes per-track-artist copies of Various Artists
# albums (e.g. 'Ryan Gosling/Barbie The Album') into the real VA album once
# MusicBrainz enrichment confirms the canonical album artist.
from .autofill_engine import shadow_album_cleanup_tick
scheduler.add_job(
    shadow_album_cleanup_tick, "interval", minutes=30,
    id="shadow_album_cleanup", max_instances=1, coalesce=True, misfire_grace_time=300,
)

# COVER IDENTITY — every album's identity is its Spotify cover/album-id (assigned
# from where it came from); rows/folders sharing one are the same album and get
# combined regardless of artist/name spelling.
from .autofill_engine import cover_identity_tick
scheduler.add_job(
    cover_identity_tick, "interval", minutes=30,
    id="cover_identity", max_instances=1, coalesce=True, misfire_grace_time=300,
)

# LIBRARY HYGIENE — keeps every album in Plex on purpose, WITHOUT moving files:
# accidental/broken albums are hidden via /Volumes/MediaVolume3/plexify-music/.plexignore; a rotating
# integrity spot-check hides corrupt files + unlocks their albums for refill.
from .autofill_engine import library_hygiene_tick
scheduler.add_job(
    library_hygiene_tick, "interval", hours=6,
    id="library_hygiene", max_instances=1, coalesce=True, misfire_grace_time=600,
)

# ALBUM RULEBOOK — rule 2 (same-artist edition merge / absorb-or-hide),
# rule 3 (one version per song per album), rule 4 (no albums from >50-song
# counterparts; existing monsters re-homed into smaller counterpart albums).
# Rule 1 (same cover image combines) lives in cover_identity_tick.
from .title_db import rebuild as _rebuild_title_db
scheduler.add_job(
    _rebuild_title_db, "interval", weeks=4,
    id="title_db_rebuild", max_instances=1, coalesce=True, misfire_grace_time=3600,
)
from .autofill_engine import album_rules_tick
scheduler.add_job(
    album_rules_tick, "interval", hours=2,
    id="album_rules", max_instances=1, coalesce=True, misfire_grace_time=600,
)

# Reconcile Plex album tiles to disk truth (merge co-located different-title
# tiles that the verify tick's title/recent passes miss). Cursor-paged.
from .autofill_engine import enforce_clean_titles_tick
scheduler.add_job(
    enforce_clean_titles_tick, "interval", hours=2,
    id="clean_titles", max_instances=1, coalesce=True, misfire_grace_time=600,
)
from .autofill_engine import plex_tile_reconcile_tick
scheduler.add_job(
    plex_tile_reconcile_tick, "interval", minutes=20,
    id="plex_tile_reconcile", max_instances=1, coalesce=True, misfire_grace_time=300,
)

# ALBUM COMPLETION sweeper — tops up PARTIAL albums (have < real total per the
# local mirror) in place, hardest on the ones closest to done (cooldown ∝ tracks
# still missing). Self-gates: yields to active video playback and only runs when a
# download lane is free, so it never competes with the picker or movie streaming.
scheduler.add_job(
    complete_albums_tick, "interval", minutes=4,
    id="complete_albums", max_instances=1, coalesce=True, misfire_grace_time=120,
)

# Zombie acquisition watchdog — runs every 60s to evict stuck entries from
# CURRENT_ACQUISITIONS and reset DB rows stuck in 'downloading' for >15min.
scheduler.add_job(
    acquisition_watchdog_tick, "interval", seconds=60,
    id="acquisition_watchdog", max_instances=1, coalesce=True,
)
# Adaptive SpotiFLAC auto-update — every 30min, only triggers an update if
# SpotiFLAC has had zero successes in the last hour AND upstream has a newer
# commit. When healthy this is a no-op.
scheduler.add_job(
    spotiflac_health_check_tick, "interval", minutes=30,
    id="spotiflac_health_check", max_instances=1, coalesce=True,
)

# T36: re-scan each tracked artist's releases (discography mode only).
# Daily cadence — catches new albums, live releases, singles automatically.
scheduler.add_job(
    discography_refresh_tick, "interval", hours=24,
    id="library_autofill_discography_refresh", max_instances=1, coalesce=True,
)

# AUTOFILL: keep local Spotify cache fresh. Tracks read from local DB in hot path.
scheduler.add_job(
    sync_spotify_liked_tracks_tick, "interval", hours=6,
    id="sync_spotify_liked_tracks_tick", max_instances=1, coalesce=True,
)

# LOCAL MIRROR: the ONLY live-Spotify caller during normal operation. Gently mirrors
# each liked-songs artist's catalog (albums + tracks w/ ISRCs) so the picker + the
# consolidation planner work from local data — never live Spotify in the acquisition path.
scheduler.add_job(
    spotify_catalog_sync_tick, "interval", minutes=3,
    id="spotify_catalog_sync", max_instances=1, coalesce=True, misfire_grace_time=180,
)

# PLEXAMP STARS: rate every Spotify-liked song that's in Plex so it shows as liked in
# Plexamp. Gentle batch every 15 min; round-robins likes not yet downloaded.
scheduler.add_job(
    star_liked_songs_in_plex_tick, "interval", minutes=15,
    id="star_liked_songs", max_instances=1, coalesce=True, misfire_grace_time=120,
)

# AUTO-STAR (feature-flagged, default off): star EVERY Plexify-placed track (liked +
# filler) so an un-star can signal "wrong file". Placer rides recently-added albums;
# the detector debounces un-stars and fires the dispute pipeline. Both no-op when
# autostar_manage_enabled != "1"; only run at all when PLEXIFY_START_SCHEDULER=1.
scheduler.add_job(
    autostar_place_tick, "interval", minutes=20,
    id="autostar_place", max_instances=1, coalesce=True, misfire_grace_time=300,
)
scheduler.add_job(
    autostar_unstar_detect_tick, "interval", minutes=10,
    id="autostar_unstar_detect", max_instances=1, coalesce=True, misfire_grace_time=120,
)

# MANUAL IMPORT (feature-flagged, default off): scan the user's import drop-folder, sort good FLAC
# into the library, discard junk (delete or quarantine per the toggle). No-op unless
# manual_import_enabled=1; only runs when PLEXIFY_START_SCHEDULER=1.
from .import_folder import manual_import_scan_tick
scheduler.add_job(
    manual_import_scan_tick, "interval", minutes=2,
    id="manual_import_scan", max_instances=1, coalesce=True, misfire_grace_time=60,
)

# PLAYLIST RECOMPILER: nightly reorder of every Plex playlist into Spotify source order
# (decoupled, from the local cache). Intensive → runs once a night at 4am.
scheduler.add_job(
    recompile_all_plex_playlists, "cron", hour=4, minute=0,
    id="recompile_plex_playlists", max_instances=1, coalesce=True, misfire_grace_time=3600,
)


# ALBUM-CONSISTENCY WATCHER — rolling re-check of /Volumes/MediaVolume3/plexify-music for albums filed/tagged
# under the WRONG album artist (the 'telehope under Blur' class); re-files them under
# the canonical album artist. SAFE: dry-run by default (album_consistency_dryrun=1)
# until verified; reversible via /data/rollback_album_fixes.py. Every 4 min.
try:
    from .album_consistency import album_consistency_tick
    scheduler.add_job(
        album_consistency_tick, "interval", minutes=4,
        id="album_consistency", max_instances=1, coalesce=True, misfire_grace_time=120,
    )
    logging.getLogger("app.main").info("album_consistency_tick scheduled (every 4 min)")
except Exception:
    logging.getLogger("app.main").exception("could not register album_consistency_tick")


# PLEX ALBUM AUDIT (telehope) — audits Plex's actual album grouping and merges
# tiles that are the same album split apart (case/punctuation variants, incremental
# adds). Dry-run by default (album_audit_apply=0); writes /data/plex_album_audit_report.txt.
try:
    from .album_consistency import plex_album_audit_tick
    scheduler.add_job(
        plex_album_audit_tick, "interval", minutes=20,
        id="plex_album_audit", max_instances=1, coalesce=True, misfire_grace_time=300,
    )
    logging.getLogger("app.main").info("plex_album_audit_tick scheduled (every 20 min)")
except Exception:
    logging.getLogger("app.main").exception("could not register plex_album_audit_tick")


# REMATCH PLEX MISSES (telehope/UI fix) â a song downloaded after its last Plex
# miss-search stays in the 24h negative cache, so the playlists tab shows it as
# "not downloaded" for up to a day. Re-match downloaded-but-unmatched tracks every 5 min.
try:
    from .autofill_engine import rematch_plex_misses_tick
    scheduler.add_job(
        rematch_plex_misses_tick, "interval", minutes=5,
        id="rematch_plex_misses", max_instances=1, coalesce=True, misfire_grace_time=120,
    )
    logging.getLogger("app.main").info("rematch_plex_misses_tick scheduled (every 5 min)")
except Exception:
    logging.getLogger("app.main").exception("could not register rematch_plex_misses_tick")

# Expose scheduler so routes can modify_job() when user changes interval (B6)
app.scheduler = scheduler

# Role guard: the Mac runs the engine in stages. PLEXIFY_START_SCHEDULER=0 serves
# the UI only (no background jobs, no Linux inotify zip_watcher) — used for the
# UI-first bring-up and when the org scheduler is managed separately. Defaults ON
# so the NAS/full behavior is unchanged.
if os.environ.get("PLEXIFY_START_SCHEDULER", "1") == "1":
    scheduler.start()
    try:
        from . import zip_watcher
        zip_watcher.start()
    except Exception:
        logging.getLogger(__name__).exception("zip_watcher.start failed (continuing)")
    logging.getLogger(__name__).info(
        "Watcher started — change-scan every %d min, full reconcile every %d min",
        SYNC_INTERVAL_MINUTES, DELETIONS_INTERVAL_MINUTES,
    )
    # Startup auto-discover: if Spotify is already authed (e.g., container restart),
    # fire one watcher tick immediately so we don't wait up to 5 minutes.
    try:
        from . import auth_spotify as _auth_spotify
        if _auth_spotify.is_authed():
            logging.getLogger(__name__).info("Spotify authed at startup — firing immediate watcher tick")
            trigger_immediate_watcher()
    except Exception:
        logging.getLogger(__name__).exception("startup watcher trigger failed")
else:
    logging.getLogger(__name__).info("PLEXIFY_START_SCHEDULER=0 — UI-only mode (scheduler + zip_watcher not started)")


