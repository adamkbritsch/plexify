# macOS app & the Mac + NAS split (advanced)

These are **advanced, build-from-source** deployments. If you just want to run Plexify, use the
Docker quick start in the [README](../README.md) — you don't need any of this.

## Native macOS app

`macapp/` is a native SwiftUI front-end for the same engine (dark, matches the web UI 1:1). It's
currently a **developer build**: it launches a local Python engine by path rather than bundling
one, so you need the engine + a venv on the machine.

**Build:**

```bash
# from the repo root, with a Python venv + engine deps installed
bash macapp/build.sh          # produces Plexify.app (ad-hoc signed)
```

`build.sh` derives the repo root from its own location; override with `PLEXIFY_ROOT`. The bundle id
defaults to `com.plexify.app`.

**Runtime env** (the app launches the engine with these — set them if the defaults don't fit):

| Var | Default | Purpose |
|---|---|---|
| `PLEXIFY_GUNICORN` | `<root>/venv/bin/gunicorn` | gunicorn binary |
| `PLEXIFY_ENGINE_DIR` | `<root>/engine-run` | engine working dir |
| `PLEXIFY_DATA_DIR` | `<root>/data` | data dir |
| `PLEXIFY_SMB_URL` | `smb://your-nas.local/Music` | SMB share to mount (split mode) |
| `PLEXIFY_SMB_MOUNT` | `/Volumes/Music` | local mount point |

Packaging a self-contained `.app` (bundling the engine + venv) is planned.

## Mac + NAS split

The split is **the exact same code** as the single-host deployment — one engine image, one
downloader daemon, no host-specific branches. The only difference is topology: the **downloader
daemon stays on the NAS** (with slskd + storage) and the **engine + UI move to a Mac**. The engine
always talks to the daemon over HTTP, so all that changes is where each half runs and a couple of
env vars.

**On the NAS (storage host):** run only the daemon from the same compose:

```bash
docker compose up -d --build plexify-downloader     # port 8788
```

**On the Mac:** run the engine — the native app (above) or `docker compose up plexify` — and point
it at the NAS + your SMB-mounted library:

- `NAS_DOWNLOADER_HOSTS=http://<nas-ip>:8788` (or set the `nas_downloader_url` config key) so the
  engine reaches the daemon.
- `PLEXIFY_SMB_URL` / `PLEXIFY_SMB_MOUNT` so the native app mounts the NAS library over SMB.

The Mac decides what to acquire and does all organization against the SMB-mounted library; the NAS
just downloads autonomously into a staging dir the Mac picks up. Full genericization of the
internal paths (currently symlinked in the image) is on the roadmap and would make this cleaner.
