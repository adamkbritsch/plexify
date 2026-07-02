# Configuration reference

Most settings are entered through the setup wizard and the Settings page and stored in the
SQLite database (`$DATA_DIR/plexify.db`). A few deployment-level knobs are environment
variables. Nothing here needs editing by hand for a normal Docker install — the wizard covers it.

## Environment variables

| Var | Default | Used by | Purpose |
|---|---|---|---|
| `DATA_DIR` | `/data` | engine, daemon | SQLite DB + secret key + token caches. Keep private. |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8787` | engine | Builds the Spotify OAuth redirect (`…/auth/spotify/callback`). **Set to the address you actually reach the UI at.** |
| `MUSIC_DIR` | — | compose | HOST path to your Plex music library (mounted to `/music`). |
| `DOWNLOADS_DIR` | — | compose | HOST path to a downloads/staging workdir (mounted to `/downloads`). |
| `ENGINE_PORT` / `DOWNLOADER_PORT` | `8787` / `8788` | compose | Host ports. |
| `NAS_DOWNLOADER_HOSTS` | `http://plexify-downloader:8788` | engine | `;`-separated downloader daemon URL(s). |
| `DOWNLOADER_TOKEN` | (empty) | engine, daemon | Optional shared bearer token. If set, also set the engine's `nas_downloader_token` config to match. |
| `STAGING_ROOT` | `/downloads_music/staging` | daemon | Where the daemon stages verified files. |
| `SYNC_INTERVAL_MINUTES` | `5` | engine | Playlist sync cadence. |
| `SECRET_KEY` | auto-generated | engine | Flask session key (persisted to `$DATA_DIR/.secret_key`). |

## Config keys (set via the wizard / Settings, stored in the DB)

**Required**

| Key | Purpose |
|---|---|
| `spotify_client_id`, `spotify_client_secret` | Your Spotify developer app credentials. |
| `plex_url`, `plex_token`, `plex_music_section_key` | Your Plex server + auth + music library section. |
| `ownership_attested` | Set by accepting the first-run agreement; **downloading is blocked until this is set.** |

**Optional**

| Key | Purpose |
|---|---|
| `slskd_url`, `slskd_api_key` | Soulseek (slskd) source. Default pre-fill `http://slskd:5030`. |
| `lidarr_url`, `lidarr_api_key` | Lidarr organizer. Default pre-fill `http://plexify-lidarr:8686`. |
| `plex_library_path` | The music folder as your Plex server sees it. |
| `spotiflac_qobuz_token` | Qobuz/Tidal token for SpotiFLAC (unlocks 24-bit hi-res). |
| `telegram_enabled`, `telegram_api_id`, `telegram_api_hash`, `telegram_session` | Telegram source (from <https://my.telegram.org>). |
| `nas_downloader_url`, `nas_downloader_token` | Override the downloader daemon location/auth. |
| `autostar_manage_enabled`, `autostar_dry_run` | The "un-star to replace" feature (default off; dry-run logs intended actions). |

## Dependencies

Python 3.12; system `ffmpeg` (audio integrity + transcode). Python deps are in
`engine-run/requirements.txt`. Note two:

- **SpotiFLAC** is pulled from git-main and its import bug is patched automatically at startup.
- **`anthropic`** powers an optional self-healing update for SpotiFLAC; it's inert unless you set `ANTHROPIC_API_KEY`.
