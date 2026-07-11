import Foundation
import Combine
import AppKit

// Talks to the bundled Python engine over the loopback API (:8787) — the same server
// that renders the web UI. Dashboard state polls ~2s; page data loads on demand. All UI
// state lives here so the SwiftUI views stay declarative. Mirrors Visionary's AppStore.
@MainActor
final class PlexifyStore: ObservableObject {
    // dashboard (polled)
    @Published var health: HealthDTO?
    @Published var live: LiveDTO?
    @Published var picker: PickerStatusDTO?
    @Published var nas: NasDownloaderDTO?
    @Published var attestation: AttestStatusDTO?   // nil = still checking (treated as not-attested)
    @Published var reward: [RewardItemDTO] = []
    @Published var rewardHasMore = true
    @Published var importRunning = false   // a manual-import scan is actively placing songs

    // library
    @Published var albums: [LibraryAlbumDTO] = []
    @Published var albumsTotal = 0
    @Published var artists: [LibraryArtistDTO] = []
    @Published var songs: [LibrarySongDTO] = []
    @Published var songsTotal = 0
    @Published var albumDetail: AlbumDetailDTO?

    // jobs
    @Published var activeJobs: [JobDTO] = []
    @Published var recentJobs: [JobDTO] = []
    @Published var jobDetail: JobDTO?

    // playlists / settings / unmatched
    @Published var playlists: [PlaylistPairDTO] = []
    @Published var playlistsSource: String?
    @Published var settings: SettingsDTO?
    @Published var unmatched: [UnmatchedTrackDTO] = []
    @Published var unmatchedTotal = 0
    @Published var suggestions: [UnmatchedSuggestionDTO] = []
    @Published var suggestorInfo: UnmatchedSuggestionsDTO?

    // control bar
    @Published var fill: FillBalanceDTO?
    @Published var autoRefresh = true
    @Published var lastRefresh: Date?

    // transient UI signal
    @Published var lastAction: String?

    let base = "http://127.0.0.1:8787"
    private var polling = false
    var dashboardVisible = true

    // MARK: - poll loop

    func start() {
        guard !polling else { return }
        polling = true
        Task { await self.loadAttestStatus() }   // eager: show the legal gate immediately if needed
        Task {
            var tick = 0
            while self.polling {
                if self.autoRefresh {
                    if tick % 3 == 0 { await self.loadAttestStatus() }        // gate state (always)
                    await self.refreshLive()
                    await self.refreshPicker()
                    if tick % 3 == 0 { await self.refreshHealth() }          // ~6s: hits Plex/Spotify
                    if self.dashboardVisible {
                        await self.refreshImportRunning()                    // cheap local flag
                        // While there's active work (a manual import placing songs, or an
                        // acquisition in flight) refresh the feed + staging EVERY tick (~2s) so
                        // new songs live-appear; otherwise fall back to the ~6s idle cadence.
                        let busy = self.importRunning
                            || (self.live?.downloading != nil)
                            || ((self.picker?.in_flight ?? 0) > 0)
                        if busy || tick % 3 == 0 { await self.refreshRewardHead() }
                        if busy || tick % 3 == 0 { await self.refreshNas() }
                        if tick % 5 == 0 { await self.loadFill() }
                    }
                    tick += 1
                }
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
    }

    func refreshAll() async {
        await loadAttestStatus()
        await refreshLive(); await refreshHealth(); await refreshPicker(); await refreshNas(fresh: true)
        await refreshRewardHead()
    }

    // Legal-use gate: the whole UI is blocked until attested.
    func loadAttestStatus() async { if let d: AttestStatusDTO = await get("/api/attest/status") { attestation = d } }
    @discardableResult
    func submitAttest() async -> Bool { let ok = await post("/api/attest"); await loadAttestStatus(); return ok }

    // MARK: - dashboard reads

    func refreshLive()   async { if let d: LiveDTO = await get("/api/dashboard/live") { live = d; lastRefresh = Date() } }
    func refreshHealth() async { if let d: HealthDTO = await get("/api/dashboard/health") { health = d } }
    func refreshPicker() async { if let d: PickerStatusDTO = await get("/api/picker/status") { picker = d } }
    // fresh=true (manual Refresh) forces a live recount of the staging/import-pending value;
    // background polls leave it off so they stay cheap.
    func refreshNas(fresh: Bool = false) async {
        let path = fresh ? "/api/nas-downloader/status?fresh=1" : "/api/nas-downloader/status"
        if let d: NasDownloaderDTO = await get(path) { nas = d }
    }
    func refreshImportRunning() async {
        let s = await manualImportStatus()
        importRunning = (s["running"] as? Bool) ?? false
    }

    // Refresh the first page of the feed, merging so the list doesn't flicker/reset scroll.
    func refreshRewardHead() async {
        if let d: RewardFeedDTO = await get("/api/dashboard/reward-feed?offset=0&limit=10"),
           let items = d.items {
            if reward.isEmpty { reward = items; rewardHasMore = items.count >= 10; return }
            // prepend any new uids ahead of the current head
            let known = Set(reward.prefix(20).map { $0.id })
            let fresh = items.filter { !known.contains($0.id) }
            if !fresh.isEmpty { reward = fresh + reward }
        }
    }

    func loadMoreReward() async {
        if let d: RewardFeedDTO = await get("/api/dashboard/reward-feed?offset=\(reward.count)&limit=10"),
           let items = d.items {
            let known = Set(reward.map { $0.id })
            reward += items.filter { !known.contains($0.id) }
            rewardHasMore = items.count >= 10
        }
    }

    // MARK: - dashboard actions

    func runPickerNow() async { await post("/api/picker/run-now"); lastAction = "Picker fired"; await refreshPicker() }
    func setPickerPaused(_ paused: Bool) async {
        await post(paused ? "/api/picker/pause" : "/api/picker/resume")
        await refreshPicker()
    }

    func loadFill() async { if let d: FillBalanceDTO = await get("/api/picker/fill-balance") { fill = d } }
    // mode = auto | manual; value 0…4 (only sent for manual).
    func setFill(mode: String, value: Int?) async {
        var body = "mode=\(mode)"
        if let value { body += "&value=\(value)" }
        await postForm("/api/picker/fill-balance", body)
        await loadFill()
    }

    // "Reconnect Spotify" — opens the engine's OAuth login in the default browser.
    func reconnectSpotify() { openInBrowser("/auth/spotify/login") }
    func openInBrowser(_ path: String) {
        if let url = URL(string: base + path) { NSWorkspace.shared.open(url) }
    }
    func openAlbumInPlex(_ id: Int) { openInBrowser("/api/album-go/\(id)") }

    // MARK: - library

    // MARK: - condense (album rulebook on demand)

    @Published var condense: CondenseStatusDTO?

    func condenseNow() async {
        guard condense?.state != "running" else { return }
        condense = CondenseStatusDTO(state: "running", phase: "Starting…")
        _ = await post("/library/condense-now")
        // poll until done/error (condense runs on the engine; ~up to 20 min for a big first pass)
        for _ in 0..<600 {
            if let d: CondenseStatusDTO = await get("/library/condense-status") {
                condense = d
                if d.state == "done" || d.state == "error" || d.state == "idle" { break }
            }
            try? await Task.sleep(nanoseconds: 2_000_000_000)
        }
        await loadAlbums()   // refresh the library view after consolidating
    }

    func loadAlbums(q: String = "", filter: String = "all", sort: String = "recent",
                    source: String = "", reset: Bool = true) async {
        let off = reset ? 0 : albums.count
        let path = "/api/library/albums?offset=\(off)&limit=24"
            + "&q=\(esc(q))&filter=\(filter)&sort=\(sort)&source=\(source)"
        if let d: LibraryAlbumsDTO = await get(path) {
            let items = d.items ?? []
            albums = reset ? items : albums + items
            albumsTotal = d.total ?? albums.count
        }
    }

    func loadArtists(q: String = "", filter: String = "all", source: String = "") async {
        let path = "/api/library/artists?q=\(esc(q))&filter=\(filter)&source=\(source)"
        if let d: LibraryArtistsDTO = await get(path) { artists = d.items ?? [] }
    }

    func loadSongs(q: String = "", reset: Bool = true) async {
        let off = reset ? 0 : songs.count
        if let d: LibrarySongsDTO = await get("/api/library/songs?offset=\(off)&limit=50&q=\(esc(q))") {
            let items = d.items ?? []
            songs = reset ? items : songs + items
            songsTotal = d.total ?? songs.count
        }
    }

    func loadAlbumDetail(_ id: Int) async {
        albumDetail = nil
        albumDetail = await get("/api/library/album/\(id)")
    }

    func albumArtURL(_ id: Int?) -> URL? {
        guard let id else { return nil }
        return URL(string: base + "/api/album-art/\(id)")
    }

    // MARK: - jobs

    func loadJobs() async {
        if let a: ActiveJobsDTO = await get("/api/jobs") { activeJobs = a.active ?? [] }
        if let r: RecentJobsDTO = await get("/api/jobs/recent?limit=50") { recentJobs = r.recent ?? [] }
    }
    func loadJobDetail(_ id: Int) async { jobDetail = await get("/api/jobs/\(id)") }

    // MARK: - audiobooks

    @Published var audiobooks: AudiobooksStatusDTO?
    @Published var audiobookSections: [PlexSectionDTO] = []

    func loadAudiobooks() async {
        if let d: AudiobooksStatusDTO = await get("/api/audiobooks/status") { audiobooks = d }
    }
    func organizeAudiobooksNow() async {
        _ = await postAction("/api/audiobooks/organize-now")
        lastAction = "Audiobook organize started"
        await loadAudiobooks()
    }
    func resolveAudiobook(file: String, asin: String? = nil,
                          author: String? = nil, title: String? = nil) async -> Bool {
        var body: [String: Any] = ["file": file]
        if let asin { body["asin"] = asin }
        if let author { body["author"] = author }
        if let title { body["title"] = title }
        let ok = await postJSON("/api/audiobooks/resolve", body)
        await loadAudiobooks()
        return ok
    }
    @Published var audiobookShelf: [AudiobookShelfItemDTO]?
    func loadAudiobookShelf() async {
        struct R: Codable { var items: [AudiobookShelfItemDTO]? }
        if let d: R = await get("/api/audiobooks/library") { audiobookShelf = d.items ?? [] }
    }
    func deleteAudiobook(relDir: String = "", dest: String = "") async -> (Bool, String?) {
        var body: [String: Any] = [:]
        if !relDir.isEmpty { body["rel_dir"] = relDir }
        if !dest.isEmpty { body["dest"] = dest }
        let (ok, err) = await postJSONChecked("/api/audiobooks/delete", body)
        if ok {
            // optimistic removal — the Plex-side cleanup takes ~30s and the shelf cache would
            // otherwise show the deleted book (with a live trash button) until then
            audiobookShelf?.removeAll { ($0.rel_dir ?? "") == relDir && !relDir.isEmpty }
        }
        await loadAudiobooks()
        return (ok, err)
    }
    func discardAudiobookReview(file: String) async -> (Bool, String?) {
        let (ok, err) = await postJSONChecked("/api/audiobooks/discard", ["file": file])
        await loadAudiobooks()
        return (ok, err)
    }
    func loadAudiobookSections() async {
        struct R: Codable { var sections: [PlexSectionDTO]? }
        if let d: R = await get("/api/plex/audiobook-sections") { audiobookSections = d.sections ?? [] }
    }
    func createAudiobookSection() async -> [String: Any] {
        let res = await postAction("/api/audiobooks/create-plex-section", timeout: 40)
        await loadAudiobookSections(); await loadSettings()
        return res
    }

    // MARK: - playlists

    func loadPlaylists() async {
        if let d: PlaylistsDTO = await get("/api/playlists") {
            playlists = d.items ?? []
            playlistsSource = d.source_name
        }
    }
    func syncAll() async { await post("/api/sync/all"); lastAction = "Sync started"; await loadJobs() }
    func syncPair(_ id: Int) async { await post("/api/sync/pair/\(id)"); await loadJobs() }

    // MARK: - settings

    func loadSettings() async { settings = await get("/api/settings") }
    func manualImportStatus() async -> [String: Any] {
        guard let url = URL(string: base + "/manual-import/status") else { return [:] }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            return ((try? JSONSerialization.jsonObject(with: data)) as? [String: Any]) ?? [:]
        } catch { return [:] }
    }
    func setLikedCover(_ which: String) async {
        _ = await postForm("/settings/liked-cover", "which=\(which)")
        lastAction = "Cover set to \(which)"; await loadSettings()
    }
    // POST an action endpoint and return its JSON body (test buttons, SpotiFLAC update).
    func postAction(_ path: String, timeout: TimeInterval = 20) async -> [String: Any] {
        guard let url = URL(string: base + path) else { return [:] }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"; req.timeoutInterval = timeout
        req.setValue("fetch", forHTTPHeaderField: "X-Requested-With")
        do {
            let (data, _) = try await URLSession.shared.data(for: req)
            return ((try? JSONSerialization.jsonObject(with: data)) as? [String: Any]) ?? [:]
        } catch { return [:] }
    }
    func saveSettings(_ body: [String: Any]) async {
        await postJSON("/api/settings", body); lastAction = "Settings saved"; await loadSettings()
    }

    // MARK: - unmatched

    func loadUnmatched() async {
        if let d: UnmatchedDTO = await get("/api/unmatched") {
            unmatched = d.rows ?? []
            unmatchedTotal = d.total ?? unmatched.count
        }
    }
    func clearUnmatched() async { await post("/unmatched/clear"); await loadUnmatched() }

    func loadSuggestions() async {
        if let d: UnmatchedSuggestionsDTO = await get("/api/unmatched/suggestions") {
            suggestorInfo = d
            suggestions = d.suggestions ?? []
        }
    }
    func dismissSuggestion(_ artistKey: String) async {
        await post("/api/unmatched/suggestions/dismiss?artist=\(esc(artistKey))")
        await loadSuggestions()
    }
    func requeueSuggestion(_ artistKey: String) async {
        await post("/api/unmatched/suggestions/requeue?artist=\(esc(artistKey))")
        lastAction = "Re-queued for auto-acquire"; await loadSuggestions()
    }

    // MARK: - transport

    private func esc(_ s: String) -> String {
        s.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? s
    }

    private func get<T: Decodable>(_ path: String) async -> T? {
        guard let url = URL(string: base + path) else { return nil }
        do {
            var req = URLRequest(url: url); req.timeoutInterval = 8
            let (data, _) = try await URLSession.shared.data(for: req)
            return try JSONDecoder().decode(T.self, from: data)
        } catch { return nil }
    }

    @discardableResult
    private func post(_ path: String) async -> Bool { await postJSON(path, [:]) }

    @discardableResult
    private func postJSON(_ path: String, _ body: [String: Any]) async -> Bool {
        guard let url = URL(string: base + path) else { return false }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        do { _ = try await URLSession.shared.data(for: req); return true }
        catch { return false }
    }

    // Like postJSON, but HONEST: the engine replies 200 with {"ok": false, "error": ...} on
    // failure, and a bare transport check reports those as success. Returns (ok, error).
    private func postJSONChecked(_ path: String, _ body: [String: Any]) async -> (Bool, String?) {
        guard let url = URL(string: base + path) else { return (false, "bad url") }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        do {
            let (data, _) = try await URLSession.shared.data(for: req)
            let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            let ok = (obj?["ok"] as? Bool) ?? false
            return (ok, ok ? nil : (obj?["error"] as? String ?? "request failed"))
        } catch { return (false, error.localizedDescription) }
    }

    // Form-encoded POST for the engine's request.form endpoints (e.g. fill-balance).
    @discardableResult
    private func postForm(_ path: String, _ body: String) async -> Bool {
        guard let url = URL(string: base + path) else { return false }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        req.setValue("fetch", forHTTPHeaderField: "X-Requested-With")
        req.httpBody = body.data(using: .utf8)
        do { _ = try await URLSession.shared.data(for: req); return true }
        catch { return false }
    }
}
