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
| `PLEXIFY_SMB_URL` | `smb://your-nas.local/Music` | SMB share to mount (split mode); comma-separated list = fallbacks |
| `PLEXIFY_SMB_MOUNT` | `/Volumes/Music` | local mount point |
| `PLEXIFY_SMB_PASS` | *(unset)* | share password — only needed if it isn't in your login Keychain |
| `PLEXIFY_ENGINE_URL` | *(unset)* | talk to an engine running elsewhere; the app then runs nothing locally |

### Two ways to run it

**Hosted engine (default).** The app launches the engine itself and keeps the library mounted for
it. Everything lives on this Mac — and so nothing happens while the Mac is asleep or the app is
closed. Note that the app starts the engine with `PLEXIFY_START_SCHEDULER=0`, so in this mode the
background jobs do *not* run on a timer; acquisition happens when you ask for it.

**Client (`PLEXIFY_ENGINE_URL` set).** The app becomes a front end and nothing else: it starts no
engine, mounts no shares, spawns no processes, and never pokes a job at launch. The engine runs
wherever you point it — ideally on the machine that holds the storage, where it can run all the
time with its scheduler on. Closing the app pauses nothing.

```bash
PLEXIFY_ENGINE_URL=http://your-nas:8787 open -a Plexify
```

Two things matter when the engine is remote. The window is a *view* — if it can't reach the engine
it says so in a banner rather than leaving the last poll on screen looking live. And the settings
you see belong to the engine's host, not to this Mac, so the paths in them are that machine's
paths.

Packaging a self-contained `.app` (bundling the engine + venv) is planned.

### How the NAS mount works (and why it never interrupts you)

The app keeps the share mounted itself — on launch, on wake, and every 90 seconds — because a
silently dead mount makes every number in the UI wrong. **It never shows a connection dialog.**

It mounts through AppleScript's `mount volume`, which drives the same system machinery Finder uses
but with no interface: it creates the mount point under `/Volumes` (root-owned, so `mount_smbfs`
can't) and resolves the password out of your login Keychain. `open smb://…` is never used — that
hands the URL to Finder, and Finder is what put up "Connect to Server", "There was a problem
connecting to the server", and stole focus every time the mount dropped.

Three things keep it quiet:

- **A reachability probe first.** ~10 ms TCP check of port 445 before any attempt, so being offline
  or away from home means no attempt at all rather than a doomed retry.
- **A 45-second watchdog.** If a mount is still waiting after 45s it's waiting on a human, so the
  request is cancelled instead of leaving a panel on your screen.
- **Ten-minute backoff per URL.** A share that fails to mount isn't retried on every 90s tick, and
  the next tick moves on to the next URL you configured.

List several URLs (comma-separated) for automatic fallback — e.g. a LAN name first and a Tailscale
address second; each check takes the first reachable one, so you get the fast path at home and the
remote path away from it. For a share macOS has no saved credential for, either connect once in
Finder with *Remember this password in my keychain*, or set `PLEXIFY_SMB_PASS` (handed to the mount
over stdin, so it never appears in `ps`).

### Launch at login, in the background

Pass `--minimized` to run the app with **no window and without stealing focus** — the engine and
polling start, but nothing appears until you click the Dock icon. To auto-start it that way at
login, add a LaunchAgent at `~/Library/LaunchAgents/com.<you>.plexify.plist`:

```xml
<plist version="1.0"><dict>
  <key>Label</key><string>com.you.plexify</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/open</string><string>-g</string>
    <string>/path/to/Plexify.app</string><string>--args</string><string>--minimized</string>
  </array>
  <key>RunAtLoad</key><true/>
</dict></plist>
```

Then `launchctl load -w ~/Library/LaunchAgents/com.you.plexify.plist`. (`open -g` launches it in the
background; `--minimized` keeps its window hidden. Click the Dock icon any time to open it.)

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
