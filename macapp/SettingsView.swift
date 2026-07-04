import SwiftUI

// Native Settings — mirrors the web Settings page section-for-section (web UI is the basis):
// Sources status, Plex & Spotify, Soulseek, SpotiFLAC, Qobuz mirror, Telegram, Self-repair,
// Downloading, Plex stars, Appearance, System & storage. Secrets are masked (only whether
// they're set); risky "gated" fields lock unless Self-repair is unlocked, exactly like the web.
struct SettingsView: View {
    @EnvironmentObject var store: PlexifyStore
    @State private var loaded = false

    // Plex & Spotify
    @State private var plexLibraryPath = "/plexify-music"
    // Soulseek
    @State private var slskdURL = ""
    @State private var slskdApiKey = ""           // secure; blank = keep saved
    @State private var hiresOnly = false
    // SpotiFLAC
    @State private var spotiflacRepo = ""
    @State private var qobuzToken = ""            // secure; blank = keep saved
    @State private var sfUpdateStatus = ""
    @State private var sfUpdating = false
    // Qobuz mirror
    @State private var squidBase = ""
    // Telegram
    @State private var telegramEnabled = false
    @State private var telegramApiId = ""
    @State private var telegramApiHash = ""       // secure
    @State private var telegramSession = ""       // secure
    @State private var telegramBot = "@BeatSpotBot"
    @State private var tgTestStatus = ""
    // Self-repair
    @State private var selfRepairBypass = false
    @State private var smartUpdateEnabled = false
    @State private var anthropicApiKey = ""       // secure
    @State private var anthropicModel = "claude-opus-4-8"
    @State private var keyTestStatus = ""
    // Downloading
    @State private var autofillEnabled = false
    @State private var pickerEnabled = false
    @State private var interval = 30
    @State private var acquisitionMode = "album"
    @State private var strictFlac = true
    @State private var allowMp3 = false
    @State private var allowCd = false
    @State private var sourceLiked = false
    @State private var sourceFollowed = false
    // Plex stars
    @State private var autostarEnabled = false
    @State private var autostarDryRun = false
    // Music import
    @State private var importEnabled = false
    @State private var importPath = "/Volumes/MediaVolume3/Downloads/music/import"
    @State private var importDelete = false
    @State private var importRequireLiked = false
    @State private var importSongsOnly = false
    @State private var importStatus = ""
    @State private var importScanning = false

    private var unlocked: Bool { selfRepairBypass || (store.settings?.self_repair_full ?? false) }

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            PageTitle(text: "Settings",
                      subtitle: "All connections + autofill config in one place. Changes take effect immediately.")
                .card()

            Group {
                sourcesStatusCard
                accountsCard
                soulseekCard
                spotiflacCard
                qobuzMirrorCard
            }
            Group {
                telegramCard
                selfRepairCard
                downloadingCard
                musicImportCard
                plexStarsCard
                appearanceCard
                storagePathsCard
            }

            HStack(spacing: 12) {
                Button { save() } label: { Text("Save all settings") }.buttonStyle(PrimaryButtonStyle())
                if let a = store.lastAction { Text(a).font(.system(size: 12)).foregroundStyle(PX.ok) }
                Spacer()
            }
        }
        .task { await load() }
    }

    // MARK: - Sections

    private var sourcesStatusCard: some View {
        Section2(title: "Sources", badge: "AUTO",
                 help: "Plexify picks the source for every album itself — Soulseek first, then the Qobuz mirror and SpotiFLAC for hi-res, then Telegram as a last resort. If one is down it leans on the others.") {
            VStack(spacing: 8) {
                srcStatus("Soulseek", "soulseek")
                srcStatus("Qobuz mirror", "squid")
                srcStatus("SpotiFLAC", "spotiflac")
                srcStatus("Telegram", "telegram")
            }
        }
    }

    private func srcStatus(_ name: String, _ key: String) -> some View {
        let svc = store.health?.services?[key]
        return HStack(spacing: 12) {
            Dot(color: healthColor(svc?.state), size: 8)
            Text(name).font(.system(size: 14, weight: .medium)).foregroundStyle(PX.text).frame(width: 130, alignment: .leading)
            Text(svc?.detail ?? "status unknown").font(.system(size: 12)).foregroundStyle(PX.muted).lineLimit(1)
            Spacer()
        }
    }

    private var accountsCard: some View {
        Section2(title: "Plex & Spotify",
                 help: "The two accounts Plexify syncs between. Each connects on its own setup page; the path below tells Plexify how YOUR Plex server sees the shared music folder.") {
            VStack(alignment: .leading, spacing: 12) {
                statusLine("Spotify", ok: store.settings?.spotify_authed,
                           detail: store.settings?.spotify_authed == true ? "connected" : "not connected")
                statusLine("Plex", ok: store.settings?.plex_token_set,
                           detail: store.settings?.plex_token_set == true ? (store.settings?.plex_url ?? "connected") : "not connected")
                FieldLabel("Music library path as Plex sees it",
                           help: "Plexify sees the folder as /plexify-music; enter the path your Plex server reports for that same folder.")
                pxTF($plexLibraryPath, "/plexify-music")
            }
        }
    }

    private var soulseekCard: some View {
        Section2(title: "Soulseek", pill: healthPill("soulseek"),
                 help: "Peer-to-peer, tried first for every album. Manage transfers on the Soulseek page.") {
            VStack(alignment: .leading, spacing: 12) {
                FieldLabel("slskd URL", help: "e.g. http://slskd:5030 if it runs in the same compose stack")
                pxTF($slskdURL, "http://slskd:5030")
                FieldLabel("slskd API key", help: "from slskd.yml; blank keeps the saved one")
                pxSF($slskdApiKey, saved: store.settings?.slskd_api_key_set == true, placeholder: "paste your slskd API key")
                Toggle(isOn: $hiresOnly) {
                    label2("Require hi-res from Soulseek",
                           "auto-relaxes to CD-quality lossless when SpotiFLAC isn't delivering")
                }.toggleStyle(.switch).tint(PX.plex)
            }
        }
    }

    private var spotiflacCard: some View {
        Section2(title: "SpotiFLAC", pill: healthPill("spotiflac"),
                 help: "The library that talks to Tidal/Qobuz/Deezer/Amazon. Auto-updates daily; update now if a source is failing.") {
            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 12) {
                    Button { updateSpotiflac() } label: { Text("Update SpotiFLAC now") }
                        .buttonStyle(GhostButtonStyle()).disabled(sfUpdating)
                    if !sfUpdateStatus.isEmpty {
                        Text(sfUpdateStatus).font(.system(size: 12)).foregroundStyle(PX.text2).lineLimit(2)
                    }
                }
                Divider().overlay(PX.line)
                FieldLabel("Qobuz / Tidal auth token",
                           help: "Optional but highly recommended — unlocks authenticated Tidal & Qobuz. Blank keeps saved.")
                pxSF($qobuzToken, saved: store.settings?.spotiflac_qobuz_token_set == true, placeholder: "paste user_auth_token")
                FieldLabel("Upstream repo", gated: !unlocked, help: "the GitHub repo the nightly update installs from")
                pxTF($spotiflacRepo, "owner/repo", disabled: !unlocked)
            }
        }
    }

    private var qobuzMirrorCard: some View {
        Section2(title: "Qobuz mirror", pill: healthPill("squid"),
                 help: "A squid.wtf-style JSON mirror of Qobuz used as a hi-res FLAC source.") {
            VStack(alignment: .leading, spacing: 10) {
                FieldLabel("Mirror base URL", gated: !unlocked)
                pxTF($squidBase, "https://qobuz.squid.wtf", disabled: !unlocked)
            }
        }
    }

    private var telegramCard: some View {
        Section2(title: "Telegram source", pill: healthPill("telegram"),
                 badge: telegramEnabled ? "ON" : "OFF",
                 help: "A last-resort FLAC source via @BeatSpotBot. Signs in with your own Telegram session string — your phone/code never leave the NAS.") {
            VStack(alignment: .leading, spacing: 12) {
                Toggle(isOn: $telegramEnabled) { label1("Enable Telegram as a source") }
                    .toggleStyle(.switch).tint(PX.plex)
                FieldLabel("API ID", help: "from my.telegram.org → API development tools")
                pxTF($telegramApiId, "1234567")
                FieldLabel("API hash", help: "blank keeps the saved one")
                pxSF($telegramApiHash, saved: store.settings?.telegram_api_hash_set == true, placeholder: "32-char hex")
                FieldLabel("Session string", help: "generated once via `python -m app.tg_login`; blank keeps saved")
                pxSF($telegramSession, saved: store.settings?.telegram_session_set == true, placeholder: "paste the StringSession")
                FieldLabel("Bot username", gated: !unlocked)
                pxTF($telegramBot, "@BeatSpotBot", disabled: !unlocked)
                HStack(spacing: 12) {
                    Button { testTelegram() } label: { Text("Test session") }.buttonStyle(GhostButtonStyle())
                    if !tgTestStatus.isEmpty {
                        Text(tgTestStatus).font(.system(size: 12)).foregroundStyle(PX.text2).lineLimit(2)
                    }
                }
            }
        }
    }

    private var selfRepairCard: some View {
        let badge = (smartUpdateEnabled && (store.settings?.anthropic_api_key_set ?? false)) ? "FULL"
                    : (selfRepairBypass ? "BYPASS" : "BASIC")
        return Section2(title: "Self-repair", badge: badge,
                 help: "Always-on failover keeps sources healthy for free. The optional AI layer repairs SpotiFLAC's nightly code updates when they break.") {
            VStack(alignment: .leading, spacing: 12) {
                Toggle(isOn: $selfRepairBypass) {
                    label2("External-AI bypass", "no API key: risky settings unlock, and saving generates a repair prompt for your AI app")
                }.toggleStyle(.switch).tint(PX.plex)
                Toggle(isOn: $smartUpdateEnabled) { label1("Enable Claude smart-repair on broken updates") }
                    .toggleStyle(.switch).tint(PX.plex)
                FieldLabel("Anthropic API key", help: "from console.anthropic.com; metered. Blank keeps saved.")
                pxSF($anthropicApiKey, saved: store.settings?.anthropic_api_key_set == true, placeholder: "sk-ant-…")
                FieldLabel("Model")
                pxTF($anthropicModel, "claude-opus-4-8")
                HStack(spacing: 12) {
                    Button { testKey() } label: { Text("Test key") }.buttonStyle(GhostButtonStyle())
                    if !keyTestStatus.isEmpty {
                        Text(keyTestStatus).font(.system(size: 12)).foregroundStyle(PX.text2).lineLimit(2)
                    }
                }
            }
        }
    }

    private var downloadingCard: some View {
        Section2(title: "Downloading", badge: autofillEnabled ? "ON" : "OFF",
                 help: "Fills your Plex library from Spotify — what gets found, how much per song, and the quality rules. Pace is automatic.") {
            VStack(alignment: .leading, spacing: 14) {
                Toggle(isOn: $autofillEnabled) { label2("Find new music", "scan Spotify and queue what's missing") }
                    .toggleStyle(.switch).tint(PX.plex)
                Toggle(isOn: $pickerEnabled) { label2("Download the queue", "the engine that fetches queued albums from your sources") }
                    .toggleStyle(.switch).tint(PX.plex)
                HStack(spacing: 12) {
                    Text("Interval").font(.system(size: 13)).foregroundStyle(PX.text2)
                    Stepper(value: $interval, in: 5...360, step: 5) {
                        Text("\(interval) min").font(.system(size: 13)).monospacedDigit().foregroundStyle(PX.text)
                    }
                }
                groupLabel("What feeds it")
                Toggle(isOn: $sourceLiked) { label1("Liked Songs") }.toggleStyle(.switch).tint(PX.plex)
                Toggle(isOn: $sourceFollowed) { label2("Followed Artists", "every album + single by every artist you follow — high volume") }
                    .toggleStyle(.switch).tint(PX.plex)
                groupLabel("How much per liked song")
                Picker("", selection: $acquisitionMode) {
                    Text("Song only").tag("song")
                    Text("Whole album").tag("album")
                    Text("Full discography").tag("discography")
                }.pickerStyle(.segmented).labelsHidden()
                groupLabel("Quality rules")
                Toggle(isOn: $strictFlac) { label2("FLAC only", "reject MP3/lossy from every source") }.toggleStyle(.switch).tint(PX.plex)
                Toggle(isOn: $allowMp3) { label2("MP3-320 as last resort", "only after every FLAC option is exhausted") }.toggleStyle(.switch).tint(PX.plex)
                Toggle(isOn: $allowCd) { label2("CD-quality for stuck albums", "let albums that gave up on hi-res land 16-bit/44.1 FLAC") }.toggleStyle(.switch).tint(PX.plex)
            }
        }
    }

    private var musicImportCard: some View {
        Section2(title: "Music import", badge: importEnabled ? "ON" : "OFF",
                 help: "Drop music into a folder and Plexify sorts the good FLAC into your library (even if it isn't a liked song), discarding the junk. This closes the gaps the automated sources can't get — see the Unmatched tab's suggestions for what to grab.") {
            VStack(alignment: .leading, spacing: 12) {
                Toggle(isOn: $importEnabled) {
                    label2("Enable music import", "auto-scans the import folder every couple of minutes")
                }.toggleStyle(.switch).tint(PX.plex)
                FieldLabel("Import folder", help: "where you drop files (on the NAS share)")
                pxTF($importPath, "/Volumes/MediaVolume3/Downloads/music/import")
                Toggle(isOn: $importDelete) {
                    label2("Delete unnecessary music", "permanently delete junk / lossy / duplicates instead of quarantining them to _unnecessary/. Off = recoverable.")
                }.toggleStyle(.switch).tint(importDelete ? PX.danger : PX.plex)
                Toggle(isOn: $importSongsOnly) {
                    label2("Keep songs only", "delete non-song files (playlists, logs, pdfs) too; album covers are kept and filed with the songs")
                }.toggleStyle(.switch).tint(PX.plex)
                Toggle(isOn: $importRequireLiked) {
                    label2("Only import music in my Spotify", "stricter — also discard FLAC that doesn't match a liked / playlist song")
                }.toggleStyle(.switch).tint(PX.plex)
                HStack(spacing: 12) {
                    Button { runImport(dry: true) } label: { Text("Preview") }
                        .buttonStyle(GhostButtonStyle()).disabled(importScanning)
                    Button { runImport(dry: false) } label: { Text("Scan now") }
                        .buttonStyle(PrimaryButtonStyle()).disabled(importScanning || !importEnabled)
                    if !importStatus.isEmpty {
                        Text(importStatus).font(.system(size: 12)).foregroundStyle(PX.text2).lineLimit(2)
                    }
                }
            }
        }
    }

    private func runImport(dry: Bool) {
        importScanning = true
        importStatus = dry ? "previewing…" : "scanning…"
        Task {
            // persist the current toggles first so the scan honors them
            await store.saveSettings([
                "manual_import_enabled": importEnabled, "manual_import_path": importPath,
                "manual_import_delete_unnecessary": importDelete,
                "manual_import_require_liked": importRequireLiked,
                "manual_import_songs_only": importSongsOnly,
            ])
            // Kick off the scan in the background (big folders exceed the HTTP timeout), then poll.
            let start = await store.postAction(dry ? "/manual-import/preview" : "/manual-import/scan", timeout: 20)
            if start["ok"] as? Bool != true {
                importStatus = (start["error"] as? String) ?? "failed"; importScanning = false; return
            }
            if start["started"] as? Bool == false {
                importStatus = "a scan is already running"; importScanning = false; return
            }
            for _ in 0..<900 {                                   // poll up to ~30 min
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                let st = await store.manualImportStatus()
                if st["running"] as? Bool == false, let d = st["result"] as? [String: Any] {
                    if d["ok"] as? Bool == true {
                        let imp = d["imported"] as? Int ?? 0, up = d["upgraded"] as? Int ?? 0
                        let del = d["deleted"] as? Int ?? 0, q = d["quarantined"] as? Int ?? 0
                        let sc = d["scanned"] as? Int ?? 0
                        let by = (d["by_reason"] as? [String: Any])?
                            .map { "\($0.key) \($0.value)" }.sorted().joined(separator: ", ") ?? ""
                        importStatus = dry
                            ? "would import \(imp)\(up > 0 ? " (+\(up) upgrade)" : "") of \(sc) scanned — \(by)"
                            : "imported \(imp), \(up) upgraded, \(del) deleted, \(q) quarantined"
                    } else {
                        importStatus = (d["error"] as? String) ?? "failed"
                    }
                    break
                }
            }
            importScanning = false
        }
    }

    private var plexStarsCard: some View {
        Section2(title: "Plex stars",
                 help: "Auto-★ every placed track. Un-star a song in Plex/Plexamp to flag it wrong — Plexify attics it, blacklists that source, and re-acquires the correct copy.") {
            VStack(alignment: .leading, spacing: 12) {
                Toggle(isOn: $autostarEnabled) { label1("Manage Plex stars") }.toggleStyle(.switch).tint(PX.plex)
                if autostarEnabled {
                    Toggle(isOn: $autostarDryRun) { label1("Dry run — log intended replacements without acting") }
                        .toggleStyle(.switch).tint(PX.plex)
                }
            }
        }
    }

    private var appearanceCard: some View {
        Section2(title: "Liked Songs cover",
                 help: "The poster for your Spotify Liked Songs playlist in Plex / Plexamp.") {
            HStack(spacing: 12) {
                coverButton("Star", "star")
                coverButton("Heart", "heart")
                Spacer()
            }
        }
    }

    private func coverButton(_ title: String, _ which: String) -> some View {
        let active = (store.settings?.liked_songs_cover ?? "star") == which
        return Button { Task { await store.setLikedCover(which) } } label: {
            Text(title).font(.system(size: 13, weight: .semibold))
                .foregroundStyle(active ? PX.plex : PX.text2)
                .padding(.horizontal, 18).padding(.vertical, 10)
                .frame(minWidth: 90)
        }
        .buttonStyle(.plain)
        .background(RoundedRectangle(cornerRadius: PX.controlRadius).fill(active ? PX.plex.opacity(0.10) : PX.bg3))
        .overlay(RoundedRectangle(cornerRadius: PX.controlRadius).stroke(active ? PX.plex : PX.line, lineWidth: 1))
    }

    private var storagePathsCard: some View {
        Section2(title: "Storage paths",
                 help: "Container-side paths are fixed; map them to your folders in .env next to docker-compose.yml.") {
            VStack(alignment: .leading, spacing: 8) {
                pathRow("/plexify-music", "MUSIC_DIR", "Your music library — the same folder your Plex music library points at")
                pathRow("/downloads_music", "DOWNLOADS_DIR", "Staging for downloads before import; also holds the reversible attics")
                pathRow("/data", "./data", "Plexify's own database, logs, and rollback ledgers")
            }
        }
    }

    private func pathRow(_ container: String, _ env: String, _ desc: String) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Text(container).font(.system(size: 12, design: .monospaced)).foregroundStyle(PX.plex).frame(width: 130, alignment: .leading)
            Text(env).font(.system(size: 12, design: .monospaced)).foregroundStyle(PX.text2).frame(width: 110, alignment: .leading)
            Text(desc).font(.system(size: 12)).foregroundStyle(PX.muted).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
    }

    // MARK: - small view helpers

    private func statusLine(_ name: String, ok: Bool?, detail: String) -> some View {
        HStack(spacing: 12) {
            Dot(color: ok == true ? PX.ok : (ok == false ? PX.danger : PX.muted), size: 8)
            Text(name).font(.system(size: 14, weight: .medium)).foregroundStyle(PX.text).frame(width: 130, alignment: .leading)
            Text(detail).font(.system(size: 13)).foregroundStyle(PX.muted).lineLimit(1)
            Spacer()
        }
    }
    private func healthPill(_ key: String) -> String? { store.health?.services?[key]?.detail }
    private func label1(_ t: String) -> some View { Text(t).font(.system(size: 14)).foregroundStyle(PX.text) }
    private func label2(_ t: String, _ sub: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(t).font(.system(size: 14)).foregroundStyle(PX.text)
            Text(sub).font(.system(size: 12)).foregroundStyle(PX.muted).fixedSize(horizontal: false, vertical: true)
        }
    }
    private func groupLabel(_ t: String) -> some View {
        Text(t.uppercased()).font(.system(size: 11, weight: .semibold)).tracking(0.5).foregroundStyle(PX.muted)
            .padding(.top, 4)
    }

    // MARK: - load / save / actions

    private func load() async { await store.loadSettings(); seed() }
    private func seed() {
        guard let s = store.settings, !loaded else { return }
        plexLibraryPath = s.plex_library_path ?? "/plexify-music"
        slskdURL = s.slskd_url ?? ""
        hiresOnly = s.hires_only ?? false
        spotiflacRepo = s.spotiflac_repo ?? ""
        squidBase = s.squid_base ?? ""
        telegramEnabled = s.telegram_enabled ?? false
        telegramApiId = s.telegram_api_id ?? ""
        telegramBot = s.telegram_bot ?? "@BeatSpotBot"
        selfRepairBypass = s.self_repair_bypass ?? false
        smartUpdateEnabled = s.smart_update_enabled ?? false
        anthropicModel = s.anthropic_model ?? "claude-opus-4-8"
        autofillEnabled = s.autofill_enabled ?? false
        pickerEnabled = s.autofill_picker_enabled ?? false
        interval = s.autofill_interval_minutes ?? 30
        acquisitionMode = s.autofill_acquisition_mode ?? "album"
        strictFlac = s.autofill_strict_flac ?? true
        allowMp3 = s.autofill_allow_mp3_fallback ?? false
        allowCd = s.autofill_allow_cd_quality ?? false
        sourceLiked = s.source_liked ?? false
        sourceFollowed = s.source_followed_artists ?? false
        autostarEnabled = s.autostar_manage_enabled ?? false
        autostarDryRun = s.autostar_dry_run ?? false
        importEnabled = s.manual_import_enabled ?? false
        importPath = s.manual_import_path ?? "/Volumes/MediaVolume3/Downloads/music/import"
        importDelete = s.manual_import_delete_unnecessary ?? false
        importRequireLiked = s.manual_import_require_liked ?? false
        importSongsOnly = s.manual_import_songs_only ?? false
        loaded = true
    }

    private func save() {
        var body: [String: Any] = [
            "plex_library_path": plexLibraryPath,
            "slskd_url": slskdURL,
            "hires_only": hiresOnly,
            "telegram_enabled": telegramEnabled,
            "telegram_api_id": telegramApiId,
            "self_repair_bypass": selfRepairBypass,
            "smart_update_enabled": smartUpdateEnabled,
            "anthropic_model": anthropicModel,
            "autofill_enabled": autofillEnabled,
            "autofill_picker_enabled": pickerEnabled,
            "autofill_interval_minutes": interval,
            "autofill_acquisition_mode": acquisitionMode,
            "autofill_strict_flac": strictFlac,
            "autofill_allow_mp3_fallback": allowMp3,
            "autofill_allow_cd_quality": allowCd,
            "source_liked": sourceLiked,
            "source_followed_artists": sourceFollowed,
            "autostar_manage_enabled": autostarEnabled,
            "autostar_dry_run": autostarDryRun,
            "manual_import_enabled": importEnabled,
            "manual_import_path": importPath,
            "manual_import_delete_unnecessary": importDelete,
            "manual_import_require_liked": importRequireLiked,
            "manual_import_songs_only": importSongsOnly,
        ]
        // gated fields — only send when unlocked (server also enforces this)
        if unlocked {
            body["squid_base"] = squidBase
            body["spotiflac_repo"] = spotiflacRepo
            body["telegram_bot"] = telegramBot
        }
        // secrets — only send when the user actually typed something (blank keeps saved)
        if !slskdApiKey.isEmpty { body["slskd_api_key"] = slskdApiKey }
        if !qobuzToken.isEmpty { body["spotiflac_qobuz_token"] = qobuzToken }
        if !telegramApiHash.isEmpty { body["telegram_api_hash"] = telegramApiHash }
        if !telegramSession.isEmpty { body["telegram_session"] = telegramSession }
        if !anthropicApiKey.isEmpty { body["anthropic_api_key"] = anthropicApiKey }
        Task {
            await store.saveSettings(body)
            slskdApiKey = ""; qobuzToken = ""; telegramApiHash = ""; telegramSession = ""; anthropicApiKey = ""
        }
    }

    private func testTelegram() {
        tgTestStatus = "testing…"
        Task {
            let d = await store.postAction("/api/telegram/test", timeout: 40)
            if d["ok"] as? Bool == true {
                let acct = d["account"] as? String ?? "authorized"
                let reach = (d["bot_reachable"] as? Bool == true) ? "· bot reachable" : "· bot NOT reachable"
                tgTestStatus = "\(acct) \(reach)"
            } else { tgTestStatus = (d["error"] as? String) ?? "not authorized" }
        }
    }
    private func testKey() {
        keyTestStatus = "testing…"
        Task {
            let d = await store.postAction("/api/smart-update/test", timeout: 40)
            keyTestStatus = (d["ok"] as? Bool == true)
                ? "key works · \(d["model"] as? String ?? "opus")"
                : ((d["error"] as? String) ?? "key invalid")
        }
    }
    private func updateSpotiflac() {
        sfUpdating = true; sfUpdateStatus = "pulling latest… (up to 3 min)"
        Task {
            let d = await store.postAction("/settings/update-spotiflac", timeout: 200)
            if d["ok"] as? Bool == true {
                let ov = d["old_version"] as? String ?? "?", nv = d["new_version"] as? String ?? "?"
                sfUpdateStatus = ov == nv ? "already latest (\(nv)); reinstalled" : "updated \(ov) → \(nv). Restarting…"
            } else { sfUpdateStatus = (d["error"] as? String) ?? "update failed" }
            sfUpdating = false
        }
    }

    // MARK: - styled fields

    private func pxTF(_ text: Binding<String>, _ placeholder: String, disabled: Bool = false) -> some View {
        TextField(placeholder, text: text)
            .textFieldStyle(.plain).font(.system(size: 13))
            .foregroundStyle(disabled ? PX.muted : PX.text)
            .padding(.horizontal, 10).padding(.vertical, 7)
            .background(RoundedRectangle(cornerRadius: PX.controlRadius).fill(PX.bg3))
            .overlay(RoundedRectangle(cornerRadius: PX.controlRadius).stroke(PX.line, lineWidth: 1))
            .disabled(disabled)
    }
    private func pxSF(_ text: Binding<String>, saved: Bool, placeholder: String) -> some View {
        SecureField(saved ? "•••••••• (saved — leave blank to keep)" : placeholder, text: text)
            .textFieldStyle(.plain).font(.system(size: 13)).foregroundStyle(PX.text)
            .padding(.horizontal, 10).padding(.vertical, 7)
            .background(RoundedRectangle(cornerRadius: PX.controlRadius).fill(PX.bg3))
            .overlay(RoundedRectangle(cornerRadius: PX.controlRadius).stroke(PX.line, lineWidth: 1))
    }
}

// MARK: - Section card + field label

private struct Section2<Content: View>: View {
    let title: String
    var pill: String? = nil
    var badge: String? = nil
    var help: String? = nil
    @ViewBuilder var content: () -> Content
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 8) {
                Text(title).font(.system(size: 15, weight: .semibold)).foregroundStyle(PX.text)
                if let badge { Badge(text: badge, tint: badge == "OFF" || badge == "BASIC" ? PX.muted : PX.sp) }
                if let pill { Text(pill).font(.system(size: 11)).foregroundStyle(PX.muted).lineLimit(1) }
                Spacer()
            }
            if let help {
                Text(help).font(.system(size: 12)).foregroundStyle(PX.muted).fixedSize(horizontal: false, vertical: true)
            }
            content()
        }.card()
    }
}

private struct FieldLabel: View {
    let text: String
    var gated: Bool = false
    var help: String? = nil
    init(_ text: String, gated: Bool = false, help: String? = nil) { self.text = text; self.gated = gated; self.help = help }
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 6) {
                Text(text).font(.system(size: 12, weight: .semibold)).foregroundStyle(PX.text2)
                if gated {
                    Text("LOCKED — unlock via Self-repair").font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(PX.warn)
                }
            }
            if let help { Text(help).font(.system(size: 11)).foregroundStyle(PX.muted).fixedSize(horizontal: false, vertical: true) }
        }.padding(.top, 2)
    }
}
