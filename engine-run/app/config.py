"""Application configuration: data paths, secret key, and environment-derived settings."""

import os
import secrets
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "plexify.db"
TOKENS_DIR = DATA_DIR / "tokens"
TOKENS_DIR.mkdir(parents=True, exist_ok=True)


def _load_or_create_secret() -> str:
    env_val = os.environ.get("SECRET_KEY")
    if env_val and env_val != "change-me-in-env":
        return env_val
    key_file = DATA_DIR / ".secret_key"
    if key_file.exists():
        return key_file.read_text().strip()
    value = secrets.token_hex(32)
    key_file.write_text(value)
    try:
        key_file.chmod(0o600)
    except OSError:
        pass
    return value


SECRET_KEY = _load_or_create_secret()
SYNC_INTERVAL_MINUTES = int(os.environ.get("SYNC_INTERVAL_MINUTES", "5"))
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8787").rstrip("/")

SPOTIFY_SCOPES = " ".join([
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-private",
    "playlist-modify-public",
    "user-library-read",      # B21: read Liked Songs (Saved Tracks)
    "user-library-modify",    # write Liked Songs — Plex rating -> Spotify like sync
    "user-follow-read",       # B21: future Followed Artists source
])

SPOTIFY_CALLBACK_PATH = "/auth/spotify/callback"
SPOTIFY_REDIRECT_URI = f"{PUBLIC_BASE_URL}{SPOTIFY_CALLBACK_PATH}"

FUZZY_DURATION_TOLERANCE_MS = 3000
FUZZY_TITLE_THRESHOLD = 86
FUZZY_ARTIST_THRESHOLD = 80
