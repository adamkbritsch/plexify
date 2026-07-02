# Plexify

**Keep your Plex music library in sync with Spotify — automatically, in lossless FLAC.**

Plexify mirrors your Spotify playlists and Liked Songs into Plex, then quietly fills the gaps: it finds the songs you've saved but don't own yet, acquires them as FLAC, tags and files them the way Plex expects, and keeps everything organized. It has a polished web UI (and an optional native macOS app), a first-run setup wizard, and a background engine that just keeps your library complete.

> [!IMPORTANT]
> **Read this first.** Plexify can acquire music from peer-to-peer and mirror sources. It is a **personal library tool** — you are responsible for how you use it, and you must have the legal right to any music you download. On first run you must accept an agreement to that effect before *any* downloading is enabled (enforced in the engine, not just the UI). Publishing or using this does not grant you any rights to copyrighted material. See [Legal](#legal).

---

## Features

- **Spotify ⇄ Plex playlist sync** — two-way, snapshot-aware; your playlists live in both places and stay in order.
- **Liked-Songs library autofill** — the songs you've saved on Spotify but don't have in Plex get acquired in FLAC and filed automatically.
- **Multi-source acquisition** — Soulseek (P2P), squid.wtf (Qobuz hi-res), SpotiFLAC (Qobuz/Tidal/Deezer/Amazon mirrors), and Telegram — all optional, tried in your configured order, with per-source blacklisting on bad results.
- **Album completion & quality upgrades** — fills partial albums and re-acquires lossless tracks at hi-res when available.
- **Un-star to replace** — 5★ every placed track in Plex; un-star a wrong one and Plexify re-acquires the correct copy from a different source (opt-in).
- **A real UI** — a fast web dashboard (library, playlists, jobs, live activity) plus an optional native macOS app.
- **Runs itself** — a scheduler handles syncing, acquisition, organization, Plex reconciliation, and hygiene in the background.

## How it works

Plexify is a Python (Flask + APScheduler) engine that talks to your Plex server and the Spotify Web API, backed by SQLite. The engine **decides what to acquire and organizes everything**; a small **downloader daemon** does the actual downloading near your storage. Both ship in one Docker image. The web UI is served by the engine; the native macOS app is an optional front-end that talks to the same JSON API.

```
Spotify ⇄ [ Plexify engine + web UI ] ⇄ Plex
                     │
                     ▼
          [ downloader daemon ] → Soulseek / squid.wtf / SpotiFLAC / Telegram
```

## Requirements

| Thing | Required? | Notes |
|---|---|---|
| **Plex Media Server** | Yes | You almost certainly already run one. Plexify reads + writes playlists and files FLACs into a library folder. |
| **Spotify Developer app** | Yes | Free, ~2 minutes — you use *your own* credentials. See [Spotify setup](#spotify-setup). |
| **Docker + Docker Compose** | Recommended | The easiest way to run it. |
| **slskd (Soulseek)** | Optional | A Soulseek source. Without it you can still use the other sources. |
| **Lidarr** | Optional | Album organization / quality management. |
| **Telegram account** | Optional | Enables the Telegram source. |

## Quick start (Docker)

```bash
git clone https://github.com/<you>/plexify.git
cd plexify
cp .env.example .env
# edit .env — set MUSIC_DIR, DOWNLOADS_DIR, and PUBLIC_BASE_URL
docker compose up -d --build
```

Then open **http://localhost:8787** and the setup wizard walks you through Spotify, Plex, optional sources, and the agreement. Done.

### Spotify setup

1. Go to <https://developer.spotify.com/dashboard> and **Create an app**.
2. Add a **Redirect URI** of exactly `PUBLIC_BASE_URL/auth/spotify/callback`
   (e.g. `http://localhost:8787/auth/spotify/callback`, or your real host if you access Plexify remotely).
3. Copy the **Client ID** and **Client Secret** into the wizard's Spotify step.

> Set `PUBLIC_BASE_URL` in `.env` to the address you actually open the UI at — the OAuth redirect is built from it, so `localhost` only works when you run setup from the same machine.

### Plex setup

The wizard auto-discovers Plex servers on your network. You provide the server URL and an auth token ([how to find your Plex token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)), and pick your music library section.

### Optional sources

- **Soulseek** — run [slskd](https://github.com/slskd/slskd) (uncomment the `slskd` service in `docker-compose.yml`), then set its URL to `http://slskd:5030` in the wizard.
- **Lidarr** — run [Lidarr](https://lidarr.audio/) (uncomment the `lidarr` service), set URL `http://plexify-lidarr:8686`.
- **Telegram** — configure `api_id`/`api_hash` (from <https://my.telegram.org>) in Settings.
- **squid.wtf** — a public Qobuz mirror; enabled by default, no setup.

## Deployment modes

Plexify runs in two shapes. **They are the same code** — one engine image, one downloader
daemon — and the *only* difference is where the engine lives. In both, the downloader runs on
the machine with your storage; the engine always talks to it over HTTP.

### 1. Single-host ("all on the NAS") — recommended

Engine + downloader + web UI on one machine. This is the Quick Start above:

```bash
docker compose up -d --build          # starts both services on this host
```

Use the built-in **web UI** at `PUBLIC_BASE_URL`.

### 2. Split ("half on a Mac")

The downloader stays on the storage host (e.g. a low-power NAS); the engine + UI move to a Mac.
Same image, same features — just two hosts:

```bash
# on the NAS (storage host):
docker compose up -d --build plexify-downloader     # daemon only

# on the Mac — either the native app…
bash macapp/build.sh && open Plexify.app            # see docs/MACOS.md
#   …or the engine in Docker, pointed at the NAS:
NAS_DOWNLOADER_HOSTS=http://<nas-ip>:8788 docker compose up -d --build plexify
```

The Mac gets a **native SwiftUI app** (same pages + features as the web UI, including the
agreement gate); the NAS just downloads. See [`docs/MACOS.md`](docs/MACOS.md) for the split
details (SMB/paths/env).

> Both the web UI and the native macOS app are front-ends to the **same engine and JSON API** —
> every feature (sync, autofill, library, the agreement gate) is identical between them.

## Configuration

The wizard covers everything most people need. For the full list of environment variables and config keys (URLs, tokens, paths, tuning), see **[`docs/CONFIGURATION.md`](docs/CONFIGURATION.md)**. All credentials are stored in a local SQLite database (`data/plexify.db`) and never leave your machine — it is gitignored and must never be committed.

## Legal

Plexify is a tool for managing **your own** music library. The acquisition sources it can connect to may let you download copyrighted material; doing so without the right to that material may be illegal where you live and may violate the terms of the services involved.

- On first run you must accept an agreement confirming you'll use Plexify legally and that you own or have the rights to what you download. **Downloading is blocked until you do**, and the block is enforced in the engine (`nas_downloader`), not just the UI.
- The maintainers do not host, distribute, or endorse the acquisition of copyrighted material, and provide no warranty. **You are solely responsible** for how you use this software and for complying with applicable law and each connected service's terms.

## Known limitations / roadmap

- **Container mount paths.** For historical reasons the engine references some absolute paths internally; the Docker image symlinks them onto clean `/music` and `/downloads` mounts, so the compose file stays tidy. Fully genericizing these paths to environment variables is the top open task — good first contribution.
- **Native macOS app** is a developer build (it launches the engine by path). Packaging a self-contained `.app` is planned.

## Contributing

Issues and PRs welcome. Please don't include any real credentials, tokens, or a populated `data/` directory in a PR — see `.gitignore`.

## License

[MIT](LICENSE).
