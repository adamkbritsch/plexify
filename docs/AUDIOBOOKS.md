# Audiobooks

Plexify can run an audiobook pipeline next to your music: drop a book (a folder of mp3s or a
single file) into a watch folder and it comes out the other end as a single chapterized,
fully-tagged `.m4b` in your Plex audiobook library — cover, narrator, description and all.

```
plexify-imports/   (unified drop folder — music and audiobooks share it)
      │  Plexify router (runs in plexify-downloader, every 60s): classifies each
      │  drop by shape and sends it down the right path — FLAC is always left
      │  for the music pipeline
      ├──────────────► finished .m4b/.m4a books go STRAIGHT to untagged/
      │                 • a bare m4b file            → one book
      │                 • a folder with one m4b      → one book, named after the folder
      │                 • a folder of SEVERAL m4bs   → a collection: every m4b becomes
      │                                                its own book (never merged)
      ▼
recentlyadded/     (books that still need converting: mp3 / aax)
      │  auto-m4b (container): merges multi-file books into one chapterized m4b
      │  (folders of mp3s ride in whole; nested rips like Book/_/*.mp3 are
      │  flattened; multi-disc trees keep disc order via "CD1 - …" names)
      ▼
untagged/
      │  Plexify organizer (runs in plexify-downloader, near storage):
      │  infer author/title → Audible catalog match (confidence-gated, never guesses)
      │  → Audnexus metadata → MP4 tags + cover → file into the library
      ▼
Audiobooks/<Author>/<Title>/<Title>.m4b   ←  your Plex "Audiobooks" library
```

The name inference understands real-world rip naming: `Author - Title (Year) - Narrator`,
`Title by Author Book N`, reading-order prefixes (`01 FW1.0 Earth Unaware`), `Book 4 - …`
series prefixes, `{edition qualifiers}` and `read by <narrator>` suffixes, run-together
CamelCase names, unicode colons, embedded ASINs (`[B0…]`), and multi-part sets
(`(Part 1 of 3)` — parts file as ordered tracks of ONE book).

Books the matcher isn't confident about wait in a **review queue** (Audiobooks page in the app)
where you pick the right edition or type the author/title yourself — nothing is ever tagged on a
guess. This replaces the manual MP3TAG step in
[seanap's guide](https://github.com/seanap/Plex-Audiobook-Guide) with automation; the merge stage
is [seanap/auto-m4b](https://github.com/seanap/auto-m4b) unchanged.

## Setup

### 1. Folders

Pick a spot for the library (what Plex indexes) and the working tree (auto-m4b's folders):

```bash
mkdir -p /your/media/Audiobooks
mkdir -p /your/downloads/audiobooks/auto-m4b/recentlyadded /your/downloads/audiobooks/review
chown -R 1000:10 /your/media/Audiobooks /your/downloads/audiobooks
```

### 2. Containers

In `.env` set `AUDIOBOOKS_DIR=/your/media/Audiobooks`, uncomment the `auto-m4b` service in
`docker-compose.yml`, and `docker compose up -d`. The `plexify-downloader` service mounts the
library at `/audiobooks` and finds auto-m4b's tree via `AUDIOBOOKS_TEMP_DIR`
(default `/downloads/audiobooks/auto-m4b`).

### 3. The Plex library

1. **Install the Audnexus agent** (no git needed on the host):
   ```bash
   docker run --rm \
     -v "/path/to/plex/config/Library/Application Support/Plex Media Server/Plug-ins:/plugins" \
     alpine/git clone --depth 1 https://github.com/djdembeck/Audnexus.bundle /plugins/Audnexus.bundle
   ```
2. **Give Plex the library folder** — add a mount to your Plex container
   (e.g. `-v /your/media/Audiobooks:/media/Audiobooks`) and recreate it. If you keep a canonical
   `docker run` command written down, update it so future recreates keep the mount.
3. **Restart Plex**, then in Plexify: Settings → Audiobooks → **Create Plex library** (or create
   it by hand: Add Library → Music → name "Audiobooks" → your folder → Advanced → Scanner
   "Plex Music Scanner", Agent "Audnexus").
4. **One-time manual checklist in Plex** (these have no reliable API):
   - Settings → Agents → Artists *and* Albums → Audiobooks: drag **Audnexus above Local Media
     Assets**.
   - The Audiobooks library → Manage Library → Edit → Advanced: uncheck everything except
     **Store track progress**; Album sorting **By Name**; Album Art **Local Files Only**.

### 4. Turn it on

Settings → Audiobooks → enable, check the paths, save. The organizer runs every minute on the
daemon; the Audiobooks page shows the pipeline (Dropped → Converting → Ready to tag → Needs
review → In library) and the review queue.

## Notes and knobs

- **Match confidence** (Settings, default 80): below it, books park for review instead of being
  tagged. The matcher also refuses same-title/different-author matches outright.
- **Books not on Audible** (rare pressings, LibriVox recordings): resolve them from the review
  queue with a manual author + title — they're filed with minimal tags and Plex's Local Media
  Assets fills in what it can.
- auto-m4b keeps the original files in `backup/` (`MAKE_BACKUP=Y`) until you're confident;
  chapters come from the source track boundaries.
- Every file the organizer moves is ledgered in the daemon's data volume
  (`audiobook_moves.jsonl` — reverse-replayable) alongside `audiobook_books.jsonl` (the UI feed).

## Rollback

- Organizer moves: replay `audiobook_moves.jsonl` in reverse (move `dst` back to `src`).
- Plex: the config volume survives recreates; recreate with your previous command to drop the
  mount. Deleting the Audiobooks library in Plex touches no files.
- The whole feature is inert with the toggle off — the daemon worker no-ops.
