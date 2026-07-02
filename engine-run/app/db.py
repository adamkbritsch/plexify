"""SQLAlchemy models and session management for the Plexify database."""

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint,
    create_engine, event, select, text, update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .config import DB_PATH


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


engine = create_engine(
    f"sqlite:///{DB_PATH}",
    future=True,
    echo=False,
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=60000")
    cur.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


class AuthToken(Base):
    """Stores OAuth state for each service. One row per service."""
    __tablename__ = "auth_tokens"
    service: Mapped[str] = mapped_column(String(32), primary_key=True)
    payload: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class AppConfig(Base):
    """Key-value store for Spotify client_id/secret etc."""
    __tablename__ = "app_config"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class PlaylistPair(Base):
    """A sync group of N playlists across services. (Table kept as `playlist_pairs` for compat.)

    Members live in `group_members` — see `members` relationship. Legacy single-service
    columns (spotify_playlist_id, etc.) are kept populated for back-compat but new code
    should iterate `members` for generality.
    """
    __tablename__ = "playlist_pairs"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    spotify_playlist_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    plex_playlist_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    plex_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Spotify's snapshot_id — opaque token that changes only when the playlist's
    # contents change. The tiny watcher compares this to skip unchanged playlists
    # without ever fetching their track lists.
    last_spotify_snapshot_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Incremental compile bookkeeping — NAS can restart mid-compile and we resume
    # from compile_offset on next watcher tick. compile_status moves
    # pending → compiling → complete (and back to compiling on snapshot_id change).
    compile_status: Mapped[str] = mapped_column(String(16), default="pending")
    compile_offset: Mapped[int] = mapped_column(Integer, default=0)
    compile_total: Mapped[int] = mapped_column(Integer, default=0)
    compile_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    compile_completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    # Bidirectional playlist sync (added 2026-05-31): JSON list of the Spotify track
    # ids last known to be in sync (the 3-way-merge baseline), plus a signature of the
    # Plex playlist's last-seen state so unchanged pairs can be skipped cheaply.
    sync_baseline_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)
    last_plex_sig: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, default=None)
    # When true, the Plex playlist is built NEWEST-first (Spotify order reversed) so the
    # most-recently-added songs appear on top. Plex-only; snapshot stays in source order.
    reverse_order: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=False)
    # How the Plex playlist is ordered (Spotify-style sort dropdown, per playlist):
    #   source | recent | title | artist | album   (default 'source' = Spotify order)
    sort_mode: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, default="source")
    # Signature of the Plex playlist's track SET at the last sort-recompile, so the smart
    # auto-recompiler only re-sorts when a song was actually added/removed (not every tick).
    last_sorted_sig: Mapped[Optional[str]] = mapped_column(String(48), nullable=True, default=None)

    members: Mapped[list["GroupMember"]] = relationship(back_populates="pair", cascade="all, delete-orphan")
    snapshots: Mapped[list["PlaylistSnapshot"]] = relationship(back_populates="pair", cascade="all, delete-orphan")
    runs: Mapped[list["SyncRun"]] = relationship(back_populates="pair", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("spotify_playlist_id", name="uq_pair_spotify"),
    )


class LocalTrack(Base):
    """The local backup of a Spotify playlist's contents.

    This is the canonical store: once a track is here, the destination mirror
    flows operate entirely from this table — no further Spotify API calls
    needed to mirror to Tidal or Plex. Spotify is only re-touched when its
    snapshot_id changes (the watcher's job).
    """
    __tablename__ = "local_tracks"
    id: Mapped[int] = mapped_column(primary_key=True)
    pair_id: Mapped[int] = mapped_column(ForeignKey("playlist_pairs.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    spotify_track_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(512))
    artist: Mapped[str] = mapped_column(String(512))
    album: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    isrc: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    __table_args__ = (
        UniqueConstraint("pair_id", "spotify_track_id", name="uq_local_pair_track"),
    )


class GroupMember(Base):
    """A single (service, playlist_id) participating in a sync group.

    Supports unlimited services. `is_writable` controls whether we ever push tracks
    here; `propagate_deletions` controls whether removals from this side flow to others.
    """
    __tablename__ = "group_members"
    id: Mapped[int] = mapped_column(primary_key=True)
    pair_id: Mapped[int] = mapped_column(ForeignKey("playlist_pairs.id"))
    service: Mapped[str] = mapped_column(String(32))
    playlist_id: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(32), default="mirror")
    is_writable: Mapped[bool] = mapped_column(Boolean, default=True)
    propagate_deletions: Mapped[bool] = mapped_column(Boolean, default=False)
    settings_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    pair: Mapped[PlaylistPair] = relationship(back_populates="members")

    __table_args__ = (
        UniqueConstraint("pair_id", "service", name="uq_member_pair_service"),
        UniqueConstraint("service", "playlist_id", name="uq_member_service_playlist"),
    )


class PlaylistSnapshot(Base):
    """The set of track IDs we last saw on each side. Used for diffing."""
    __tablename__ = "playlist_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    pair_id: Mapped[int] = mapped_column(ForeignKey("playlist_pairs.id"))
    service: Mapped[str] = mapped_column(String(16))
    track_ids_json: Mapped[str] = mapped_column(Text)
    taken_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    pair: Mapped[PlaylistPair] = relationship(back_populates="snapshots")

    __table_args__ = (UniqueConstraint("pair_id", "service", name="uq_snap_pair_service"),)


class TrackMapping(Base):
    """Cache of spotify_track_id <-> tidal_track_id matches."""
    __tablename__ = "track_mappings"
    id: Mapped[int] = mapped_column(primary_key=True)
    spotify_track_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    tidal_track_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    plex_track_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    isrc: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(512))
    artist: Mapped[str] = mapped_column(String(512))
    method: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[int] = mapped_column(Integer, default=100)
    # When we LAST attempted to find this track in Plex. Set even on a miss
    # so we can skip re-searching for some period. Re-search if older than ~24h
    # (the user might have added FLAC files via Lidarr in the meantime).
    plex_searched_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    __table_args__ = (UniqueConstraint("spotify_track_id", "tidal_track_id", name="uq_track_pair"),)


class UnmatchedTrack(Base):
    """Track on source service we couldn't find on target service."""
    __tablename__ = "unmatched_tracks"
    id: Mapped[int] = mapped_column(primary_key=True)
    pair_id: Mapped[int] = mapped_column(ForeignKey("playlist_pairs.id"))
    source_service: Mapped[str] = mapped_column(String(16))
    target_service: Mapped[str] = mapped_column(String(16))
    source_track_id: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(512))
    artist: Mapped[str] = mapped_column(String(512))
    album: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    isrc: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    reason: Mapped[str] = mapped_column(String(255))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (UniqueConstraint("pair_id", "source_service", "source_track_id", name="uq_unmatched"),)


class Job(Base):
    """A long-running background operation visible to the user."""
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))
    # D-8: index for /jobs?pair=N scans; ON DELETE SET NULL for orphan-safe cleanup.
    pair_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("playlist_pairs.id", ondelete="SET NULL"),
        index=True, nullable=True,
    )
    status: Mapped[str] = mapped_column(String(32), default="pending")
    title: Mapped[str] = mapped_column(String(255), default="")
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    progress_current: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    progress_step: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    result_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    events: Mapped[list["JobEvent"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class JobEvent(Base):
    """A single log event within a job (for live streaming UI)."""
    __tablename__ = "job_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"))
    ts: Mapped[datetime] = mapped_column(DateTime, default=_now)
    level: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text)
    job: Mapped[Job] = relationship(back_populates="events")


class SyncRun(Base):
    """One sync attempt for one playlist pair."""
    __tablename__ = "sync_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    pair_id: Mapped[int] = mapped_column(ForeignKey("playlist_pairs.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="running")
    added_to_spotify: Mapped[int] = mapped_column(Integer, default=0)
    added_to_tidal: Mapped[int] = mapped_column(Integer, default=0)
    removed_from_tidal: Mapped[int] = mapped_column(Integer, default=0)
    added_to_plex: Mapped[int] = mapped_column(Integer, default=0)
    removed_from_plex: Mapped[int] = mapped_column(Integer, default=0)
    plex_misses: Mapped[int] = mapped_column(Integer, default=0)
    unmatched: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pair: Mapped[PlaylistPair] = relationship(back_populates="runs")


class AutofillAction(Base):
    """One row per (artist, album) considered for library autofill.

    B5: uniqueness is on the NORMALIZED keys (lowercase + diacritic-stripped),
    so 'Beyoncé' and 'Beyonce' don't create duplicate rows.

    Status values:
      - 'queued'                : POSTed to Lidarr, expected to download
      - 'downloading'           : reconciler saw it in Lidarr's queue
      - 'imported'              : Lidarr has track files for this album
      - 'failed'                : add_album POST raised; throttled retry
      - 'lookup_empty'          : Lidarr's /album/lookup returned nothing
      - 'lookup_low_confidence' : best match below threshold
      - 'abandoned'             : >= MAX_ATTEMPTS consecutive non-success
      - 'excluded'              : user manually opted out
    """
    __tablename__ = "autofill_actions"
    id: Mapped[int] = mapped_column(primary_key=True)
    artist: Mapped[str] = mapped_column(String(512))             # original
    album: Mapped[str] = mapped_column(String(512))              # original
    artist_key: Mapped[str] = mapped_column(String(512), index=True, default="")  # B5
    album_key: Mapped[str] = mapped_column(String(512), default="")               # B5
    status: Mapped[str] = mapped_column(String(32), default="queued")
    foreign_album_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    cover_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)  # the album's
    # Spotify cover image — the album's true IDENTITY for grouping/dedupe (assigned
    # from where the album came from, independent of artist/name strings)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    track_ids_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    # Truth-tracking (added 2026-05-24): snapshot of Lidarr's trackFileCount when
    # this row was created. The reconciler can then tell "files arrived BECAUSE of
    # us" (current > snapshot) vs "Lidarr already had files from the existing
    # library scan" (current == snapshot).
    pre_existing_files: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    # T35: JSON list of absolute file paths the picker wrote into /Volumes/MediaVolume3/plexify-music.
    # Used by apply_mode_to_library() to know what to prune when scope changes.
    imported_paths: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)
    total_size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    quality_acquired: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, default=None)
    hires_upgrade_attempted: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=False)
    # Provenance (added 2026-05-30): which backend actually delivered the files.
    #   source        : 'soulseek' | 'spotiflac'  (canonical, drives dashboard chips)
    #   source_detail : human-readable — for spotiflac, the mirror + SpotiFLAC version
    #                   (e.g. "qobuz · SpotiFLAC v1.4 a1b2c3d"); for soulseek, the peer.
    source: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, default=None)
    source_detail: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, default=None)
    # True if this album was re-acquired at a HIGHER quality (CD/lossless → hi-res), so
    # the dashboard can flag it as an upgrade in Recently Added.
    was_upgraded: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=False)
    # Album-completion sweeper (added 2026-05-31): when we last tried to fill in this
    # album's missing tracks, and how many times. Cooldown scales with how many tracks
    # are still missing — fewer missing ⇒ shorter cooldown ⇒ retried harder/more often.
    complete_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, default=None)
    complete_attempts: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=0)
    # Consecutive completion attempts that added ZERO new tracks — i.e. the album's
    # remaining "missing" tracks are all songs we already have elsewhere (global dedup).
    # Once this hits the cap the album stops being retried, so we don't re-download it forever.
    complete_zero_streak: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=0)

    __table_args__ = (
        UniqueConstraint("artist_key", "album_key", name="uq_autofill_keys"),
    )


def _migrate_add_columns(table: str, columns: dict[str, str]) -> None:
    """SQLite-safe additive migration: ALTER TABLE ADD COLUMN if not present."""
    with engine.begin() as conn:
        existing = {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()}
        for col, ddl in columns.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))




class SpotifyLikedTrack(Base):
    """Local mirror of the user's Spotify Liked Songs. Synced periodically by
    sync_spotify_liked_tracks_tick so the engine never has to call Spotify in
    the hot path. Stale-while-revalidate: when Spotify is 429ing, ticks keep
    succeeding off this cache."""
    __tablename__ = "spotify_liked_track"
    spotify_track_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    artist: Mapped[str] = mapped_column(String(512))
    album: Mapped[str] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(String(512))
    isrc: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # Spotify track length (homonym guard)
    primary_artist_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    added_at_spotify: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    cached_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    # Plexamp "star"/like sync: when we've set this liked song's Plex rating (and the
    # last time we tried — used to round-robin retries for songs not yet in Plex).
    plex_starred_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, default=None)
    plex_star_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, default=None)


class AutoStar(Base):
    """Tracks Plexify has auto-★'d (5-star) in Plex — source of truth for the
    'un-star = replace the wrong file' feature. One row per Plexify-placed Plex track
    (liked songs AND album-completion filler), keyed by the bare numeric ratingKey.
    The un-star detector diffs the live 5★ set against this table; the two rating-sync
    ticks read it to avoid pushing filler to Spotify Liked / stripping our stars."""
    __tablename__ = "auto_star"
    id: Mapped[int] = mapped_column(primary_key=True)
    plex_track_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # bare ratingKey
    row_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)  # AutofillAction.id → dispute_file
    spotify_track_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)  # set=liked → dispute_song; null=filler
    file_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # LOCAL-prefixed absolute path
    artist: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    album: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    title: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    starred_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    last_seen_starred_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, default=None)
    miss_count: Mapped[int] = mapped_column(Integer, default=0)          # consecutive un-starred detections (debounce)
    first_missing_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, default=None)


class AlbumArtCache(Base):
    """Server-side cache of Plex album thumbnails.
    Keyed by normalized (artist, album); image bytes stored inline.
    Fetched lazily by /api/album-art/<autofill_action_id>."""
    __tablename__ = "album_art_cache"
    artist_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    album_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    image_bytes: Mapped[bytes] = mapped_column(LargeBinary)
    content_type: Mapped[str] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class SpotifyArtistSync(Base):
    """Per-artist catalog-sync progress so the gentle background mirror is resumable.
    One row per artist the user has liked songs from. (Added 2026-05-30 — local-mirror
    consolidation: the picker/planner must work from local data, never live Spotify.)"""
    __tablename__ = "spotify_artist_sync"
    artist_key: Mapped[str] = mapped_column(String(512), primary_key=True)   # normalized
    artist_name: Mapped[str] = mapped_column(String(512))
    spotify_artist_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    liked_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|done|error
    albums_synced: Mapped[int] = mapped_column(Integer, default=0)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class SpotifyAlbum(Base):
    """Local mirror of a Spotify album's metadata (the catalog, not the user's library)."""
    __tablename__ = "spotify_album"
    album_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(512))
    album_type: Mapped[str] = mapped_column(String(32))            # album|single|compilation
    album_artist: Mapped[str] = mapped_column(String(512))
    album_artist_key: Mapped[str] = mapped_column(String(512), index=True, default="")
    total_tracks: Mapped[int] = mapped_column(Integer, default=0)
    release_date: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    image_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class SpotifyAlbumTrack(Base):
    """One track within a mirrored album. ISRC is the join key to liked songs."""
    __tablename__ = "spotify_album_track"
    id: Mapped[int] = mapped_column(primary_key=True)
    album_id: Mapped[str] = mapped_column(String(64), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    track_id: Mapped[str] = mapped_column(String(64), index=True)
    isrc: Mapped[Optional[str]] = mapped_column(String(32), index=True, nullable=True)
    title: Mapped[str] = mapped_column(String(512))
    title_key: Mapped[str] = mapped_column(String(512), index=True, default="")  # normalized fallback match

    __table_args__ = (UniqueConstraint("album_id", "track_id", name="uq_album_track"),)


class MbAlbumMeta(Base):
    """Local cache of authoritative album metadata from MusicBrainz (the 'mdb').

    Keyed by the normalized (artist, album) of an AutofillAction row. MusicBrainz
    knows the canonical album artist outright — it credits soundtracks/compilations
    to 'Various Artists' and carries Soundtrack/Compilation release-group types — so
    the picker reads the answer from here instead of inferring it from album-name
    keywords. Populated ONLY by the gentle background mb_enrich_tick (≤1 live MB
    request/sec); the acquisition hot path reads this table, never the network."""
    __tablename__ = "mb_album_meta"
    artist_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    album_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    query_artist: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    query_album: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    album_artist: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    album_artist_mbid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    is_various: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=False)
    release_group_mbid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    primary_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    secondary_types: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)  # csv
    score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # ok|notfound|error|pending
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    fetched_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class AcquiredTrack(Base):
    """Global ledger of every SONG we have already acquired — the dedup source of truth.

    One row per distinct song (not per album), so a track that appears on several
    compilations is only ever downloaded ONCE. Matched two ways:
      • isrc           — the exact recording (strongest; survives album/compilation moves)
      • tkey           — normalized 'artist\\x1ftitle' (catches the same song across releases
                          even when ISRC tags are missing/stripped)
    `quality_rank` (0..3, see autofill_engine._quality_rank) gates re-acquisition: a song is
    only fetched again if the new copy is a STRICTLY higher rank (a genuine upgrade)."""
    __tablename__ = "acquired_track"
    id: Mapped[int] = mapped_column(primary_key=True)
    tkey: Mapped[str] = mapped_column(String(512), index=True, default="")
    isrc: Mapped[Optional[str]] = mapped_column(String(32), index=True, nullable=True)
    artist: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    title: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    quality_rank: Mapped[int] = mapped_column(Integer, default=0, index=True)
    quality: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


def init_db() -> None:
    Base.metadata.create_all(engine)
    _migrate_add_columns("playlist_pairs", {
        "plex_playlist_key": "TEXT",
        "plex_enabled": "INTEGER DEFAULT 0",
        "last_spotify_snapshot_id": "TEXT",
        "sync_baseline_json": "TEXT",
        "last_plex_sig": "VARCHAR(64)",
        "reverse_order": "INTEGER DEFAULT 0",
        "sort_mode": "VARCHAR(16) DEFAULT 'source'",
        "last_sorted_sig": "VARCHAR(48)",
        "last_changed_at": "TIMESTAMP",
        "compile_status": "TEXT DEFAULT 'pending'",
        "compile_offset": "INTEGER DEFAULT 0",
        "compile_total": "INTEGER DEFAULT 0",
        "compile_started_at": "TIMESTAMP",
        "compile_completed_at": "TIMESTAMP",
    })
    _migrate_add_columns("track_mappings", {
        "plex_track_key": "TEXT",
        "plex_searched_at": "TIMESTAMP",
    })
    _migrate_add_columns("spotify_liked_track", {"duration_ms": "INTEGER"})
    _migrate_add_columns("sync_runs", {
        "added_to_plex": "INTEGER DEFAULT 0",
        "removed_from_plex": "INTEGER DEFAULT 0",
        "plex_misses": "INTEGER DEFAULT 0",
    })
    # B5: backfill normalized keys for existing AutofillAction rows
    _migrate_add_columns("autofill_actions", {
        "artist_key": "TEXT DEFAULT ''",
        "album_key": "TEXT DEFAULT ''",
    })
    _migrate_add_columns("autofill_actions", {"quality_acquired": "VARCHAR(32)", "hires_upgrade_attempted": "INTEGER DEFAULT 0"})
    _migrate_add_columns("autofill_actions", {"cover_url": "VARCHAR(512)"})
    _migrate_add_columns("spotify_album", {"image_url": "VARCHAR(512)"})
    _migrate_add_columns("autofill_actions", {"source": "VARCHAR(32)", "source_detail": "VARCHAR(128)"})
    _migrate_add_columns("autofill_actions", {"complete_attempt_at": "TIMESTAMP", "complete_attempts": "INTEGER DEFAULT 0"})
    _migrate_add_columns("autofill_actions", {"was_upgraded": "INTEGER DEFAULT 0"})
    # Dedup: consecutive completion attempts that added ZERO new tracks (everything the
    # album still "needs" is a song we already have elsewhere). Caps wasteful re-downloads.
    _migrate_add_columns("autofill_actions", {"complete_zero_streak": "INTEGER DEFAULT 0"})
    _migrate_add_columns("spotify_liked_track", {"plex_starred_at": "TIMESTAMP", "plex_star_checked_at": "TIMESTAMP"})
    _backfill_autofill_keys()
    _backfill_group_members()
    with session_scope() as s:
        s.execute(
            update(SyncRun)
            .where(SyncRun.status == "running")
            .values(status="aborted", finished_at=_now(), error_message="container restarted mid-sync")
        )
        # Same treatment for Job rows — without this, the dashboard's Active Jobs
        # panel + global toast keep displaying a job that died with the container.
        s.execute(
            update(Job)
            .where(Job.status.in_(["pending", "running"]))
            .values(status="aborted", finished_at=_now(), message="orphaned by container restart")
        )


def _backfill_autofill_keys() -> None:
    """B5: populate artist_key/album_key for rows added before normalized
    uniqueness landed. Idempotent — only updates empty key fields."""
    import unicodedata
    def norm(s):
        if not s:
            return ''
        s = unicodedata.normalize('NFKD', s)
        s = ''.join(ch for ch in s if not unicodedata.combining(ch))
        return ' '.join(s.lower().split())
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT id, artist, album, artist_key, album_key FROM autofill_actions"
        )).fetchall()
        for row in rows:
            if not row[3] or not row[4]:
                conn.execute(
                    text("UPDATE autofill_actions SET artist_key=:ak, album_key=:bk WHERE id=:id"),
                    {'ak': norm(row[1]), 'bk': norm(row[2]), 'id': row[0]},
                )


def _backfill_group_members() -> None:
    """For each PlaylistPair, ensure a GroupMember row exists per non-null legacy service ID."""
    with session_scope() as s:
        pairs = list(s.scalars(select(PlaylistPair)).all())
        existing_keys = {
            (m.pair_id, m.service)
            for m in s.scalars(select(GroupMember)).all()
        }
        for p in pairs:
            legacy = [
                ("spotify", p.spotify_playlist_id, {"is_writable": True, "propagate_deletions": True}),
            ]
            if p.plex_enabled and p.plex_playlist_key:
                legacy.append(("plex", p.plex_playlist_key, {
                    "is_writable": True, "propagate_deletions": False,
                    "settings_json": '{"flac_only": true}',
                }))
            for service, pid, opts in legacy:
                if not pid:
                    continue
                if (p.id, service) in existing_keys:
                    continue
                s.add(GroupMember(
                    pair_id=p.id,
                    service=service,
                    playlist_id=pid,
                    role="source" if opts.get("propagate_deletions") else "mirror",
                    is_writable=opts.get("is_writable", True),
                    propagate_deletions=opts.get("propagate_deletions", False),
                    settings_json=opts.get("settings_json"),
                ))


@contextmanager
def session_scope():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_config(key: str, default: Optional[str] = None) -> Optional[str]:
    with session_scope() as s:
        row = s.scalar(select(AppConfig).where(AppConfig.key == key))
        return row.value if row else default


def set_config(key: str, value: str) -> None:
    with session_scope() as s:
        row = s.scalar(select(AppConfig).where(AppConfig.key == key))
        if row:
            row.value = value
        else:
            s.add(AppConfig(key=key, value=value))
