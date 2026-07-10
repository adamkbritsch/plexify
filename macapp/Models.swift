import Foundation

// All DTOs are fully optional so a missing/renamed field never fails decoding —
// the UI degrades gracefully instead. Mirrors the engine's JSON verbatim.

// MARK: - Dashboard health (/api/dashboard/health)

struct ServiceHealth: Codable, Hashable {
    var state: String?          // green | yellow | red | unknown
    var detail: String?
    var name: String?           // present on the four source pills
    var order: Int?
    var about: String?
    var imports_1h: Int?
}

struct HealthDTO: Codable {
    var overall: String?
    var services: [String: ServiceHealth]?
}

// MARK: - Live activity (/api/dashboard/live)

struct AcquisitionDTO: Codable, Identifiable, Hashable {
    var artist: String?
    var album: String?
    var spotify_url: String?
    var started_at: String?
    var row_id: Int?
    var mode: String?
    var tracks_done: Int?
    var tracks_total: Int?
    var quality_target: String?
    var quality_acquired: String?
    var source: String?
    var upgrading: Bool?
    var elapsed_seconds: Int?
    var eta_seconds: Int?
    var song_names: [String]?
    var id: Int { row_id ?? 0 }
}

struct RecentOutcomeDTO: Codable, Hashable {
    var artist: String?
    var album: String?
    var status: String?          // ok | error | partial | ...
    var source: String?
    var upgrading: Bool?
    var row_id: Int?
}

struct LiveDTO: Codable {
    var downloading: AcquisitionDTO?
    var idle: Bool?
    var queue_depth: Int?
    var next_picker_tick_in_seconds: Int?
    var downloads: [AcquisitionDTO]?
    var recent_outcomes: [RecentOutcomeDTO]?
}

// MARK: - Reward feed (/api/dashboard/reward-feed)

struct RewardItemDTO: Codable, Identifiable, Hashable {
    var uid: String?
    var id_: Int?
    var artist: String?
    var album: String?
    var song: String?
    var name: String?
    var liked: Bool?
    var is_liked: Bool?
    var source: String?
    var source_detail: String?
    var was_upgraded: Bool?
    var imported_at: String?
    var codec: String?
    var bits: Int?
    var sample_rate: Int?
    var quality_tier: String?     // hires | cd | lossless | lossy
    var quality_label: String?
    var size_bytes: Int?
    var duration_s: Int?
    var id: String { uid ?? "\(id_ ?? 0)-\(name ?? "")" }
    enum CodingKeys: String, CodingKey {
        case uid, artist, album, song, name, liked, is_liked, source, source_detail
        case was_upgraded, imported_at, codec, bits, sample_rate, quality_tier
        case quality_label, size_bytes, duration_s
        case id_ = "id"
    }
}

struct RewardFeedDTO: Codable {
    var items: [RewardItemDTO]?
    var offset: Int?
    var limit: Int?
}

// MARK: - Picker status (/api/picker/status)

struct CooldownDTO: Codable {
    var in_cooldown: Bool?
    var until: String?
    var seconds_remaining: Int?
}

struct PickerStatusDTO: Codable {
    var paused: Bool?
    var next_run_in_seconds: Int?
    var max_instances: Int?
    var tick_interval_seconds: Int?
    var in_flight: Int?
    var queue_depth: Int?
    var cooldown: CooldownDTO?
    var streaming_paused: Bool?
    var streaming_sessions: Int?
    var concurrency_ceiling: Int?
    var plex_coverage_pct: Int?
    var gaps_requeued_last: Int?
}

// MARK: - Library (/api/library/*)

struct LibraryAlbumDTO: Codable, Identifiable, Hashable {
    var id: Int?
    var artist: String?
    var album: String?
    var track_count: Int?
    var total_tracks: Int?
    var completed: Bool?
    var size_bytes: Int?
    var source: String?
    var is_liked: Bool?
    var locked: Bool?
    var imported_at: String?
}

struct LibraryAlbumsDTO: Codable {
    var items: [LibraryAlbumDTO]?
    var offset: Int?
    var limit: Int?
    var total: Int?
}

// Condense (/library/condense-{now,status}) — runs the album rulebook on demand
struct CondenseStatusDTO: Codable {
    var state: String?      // idle | running | done | error
    var phase: String?
    var merged: Int?
    var deduped: Int?
    var rehomed: Int?
    var hidden: Int?
    var tiles: Int?
    var error: String?
}

struct LibraryArtistDTO: Codable, Identifiable, Hashable {
    var artist: String?
    var albums: Int?
    var completed_albums: Int?
    var id: Int?
    var uid: String { artist ?? "\(id ?? 0)" }
}

struct LibraryArtistsDTO: Codable {
    var items: [LibraryArtistDTO]?
    var total: Int?
}

struct LibrarySongDTO: Codable, Identifiable, Hashable {
    var track_id: String?
    var title: String?
    var artist: String?
    var album: String?
    var duration: Int?          // ms (liked-track duration)
    var on_disk: Bool?
    var source: String?
    var row_id: Int?
    // album-detail track fields
    var track_no: Int?
    var uid: String { track_id ?? "\(row_id ?? 0)-\(title ?? "")-\(track_no ?? 0)" }
    var id: String { uid }
}

struct LibrarySongsDTO: Codable {
    var items: [LibrarySongDTO]?
    var offset: Int?
    var limit: Int?
    var total: Int?
}

// Album detail (/api/library/album/<id>): {row_id, artist, album, total_tracks?, completed?,
// locked, tracks:[{title, track_no, duration, ...}]}.
struct AlbumDetailDTO: Codable {
    var row_id: Int?
    var artist: String?
    var album: String?
    var track_count: Int?
    var total_tracks: Int?
    var completed: Bool?
    var locked: Bool?
    var source: String?
    var size_bytes: Int?
    var tracks: [LibrarySongDTO]?
}

// Fill balance (/api/picker/fill-balance)
struct FillBalanceDTO: Codable {
    var mode: String?           // auto | manual
    var value: Int?             // 0…4
    var queued: Int?
    var filling: Int?
    var fill_per_4: Int?
    var acquire_per_4: Int?
}

// MARK: - Jobs (/api/jobs, /api/jobs/<id>, /api/jobs/recent)

struct JobEventDTO: Codable, Identifiable, Hashable {
    var id: Int?
    var ts: String?
    var level: String?
    var message: String?
}

struct JobDTO: Codable, Identifiable, Hashable {
    var id: Int?
    var kind: String?
    var title: String?
    var status: String?
    var message: String?
    var progress_current: Int?
    var progress_total: Int?
    var progress_step: String?
    var started_at: String?
    var finished_at: String?
    var pair_id: Int?
    var events: [JobEventDTO]?
    var fraction: Double {
        let t = Double(progress_total ?? 0)
        guard t > 0 else { return 0 }
        return min(1, Double(progress_current ?? 0) / t)
    }
}

struct ActiveJobsDTO: Codable { var active: [JobDTO]? }
struct RecentJobsDTO: Codable { var recent: [JobDTO]? }

// MARK: - Legal-use attestation (/api/attest/status)

struct AttestStatusDTO: Codable {
    var attested: Bool?
    var attested_at: String?
}

// MARK: - NAS downloader daemon (/api/nas-downloader/status)

struct NasJobDTO: Codable, Identifiable, Hashable {
    var artist: String?
    var album: String?
    var title: String?
    var source: String?
    var id: String { "\(artist ?? "")-\(album ?? "")-\(title ?? "")" }
}

struct NasDownloaderDTO: Codable {
    var reachable: Bool?
    var queued: Int?
    var running: Int?
    var ready: Int?
    var failed: Int?
    var import_pending: Int?   // settled manual-import drops awaiting organization
    var jobs: [NasJobDTO]?
    var error: String?
}

// MARK: - Playlists (/api/playlists — new endpoint)

struct PlaylistPairDTO: Codable, Identifiable, Hashable {
    var id: Int?
    var name: String?
    var spotify_name: String?
    var spotify_id: String?
    var plex_name: String?
    var track_count: Int?
    var in_plex: Int?
    var mirrored: Bool?
    var last_synced_at: String?
    var status: String?
}

struct PlaylistsDTO: Codable {
    var items: [PlaylistPairDTO]?
    var source_name: String?      // the Spotify account name shown in the source banner
    var total: Int?
}

// MARK: - Settings (/api/settings — new endpoint)

struct SettingsDTO: Codable {
    var autofill_enabled: Bool?
    var autofill_interval_minutes: Int?
    var quality_target: String?
    var sources: [String]?                 // enabled catalog sources (liked / followed / playlist:*)
    var source_priority: [String]?         // download-source order
    var hires_only: Bool?
    var pause_when_streaming: Bool?
    var fill_mode: String?
    var fill_balance: Int?
    var slskd_url: String?
    var lidarr_url: String?
    var plex_url: String?
    var plex_token_set: Bool?
    var telegram_enabled: Bool?
    var telegram_configured: Bool?
    var squid_enabled: Bool?
    var spotify_authed: Bool?
    var nas_downloader_url: String?
    var autostar_manage_enabled: Bool?
    var autostar_dry_run: Bool?
    var ownership_attested: Bool?
    // Connections (secrets masked → *_set booleans)
    var plex_library_path: String?
    var slskd_api_key_set: Bool?
    var squid_base: String?
    var spotiflac_repo: String?
    var spotiflac_qobuz_token_set: Bool?
    var telegram_api_id: String?
    var telegram_api_hash_set: Bool?
    var telegram_session_set: Bool?
    var telegram_bot: String?
    // Self-repair
    var self_repair_bypass: Bool?
    var smart_update_enabled: Bool?
    var anthropic_api_key_set: Bool?
    var anthropic_model: String?
    var self_repair_full: Bool?
    // Downloading
    var autofill_picker_enabled: Bool?
    var autofill_acquisition_mode: String?
    var autofill_strict_flac: Bool?
    var autofill_allow_mp3_fallback: Bool?
    var autofill_allow_cd_quality: Bool?
    var source_liked: Bool?
    var source_followed_artists: Bool?
    // Appearance
    var liked_songs_cover: String?
    // Manual import
    var manual_import_enabled: Bool?
    var manual_import_path: String?
    var manual_import_delete_unnecessary: Bool?
    var manual_import_dry_run: Bool?
    var manual_import_require_liked: Bool?
    var manual_import_songs_only: Bool?
    // Audiobooks
    var audiobook_enabled: Bool?
    var audiobook_drop_path: String?
    var audiobook_library_path: String?
    var plex_audiobook_section_key: String?
    var audiobook_min_confidence: String?
}

// MARK: - Audiobooks (/api/audiobooks/*)

struct AudiobookCandidateDTO: Codable, Hashable {
    var asin: String?
    var title: String?
    var authors: [String]?
}

struct AudiobookDTO: Codable, Identifiable, Hashable {
    var ts: String?
    var status: String?          // organized | review
    var file: String?
    var title: String?
    var author: String?
    var asin: String?
    var cover_url: String?
    var score: Int?
    var dest: String?
    var reason: String?
    var guess: [String: String]?
    var candidates: [AudiobookCandidateDTO]?
    var id: String { "\(ts ?? "")-\(file ?? title ?? "")" }
}

struct AudiobooksStatusDTO: Codable {
    var reachable: Bool?
    var enabled: Bool?           // daemon-side flag
    var feature_enabled: Bool?   // engine-side flag (the settings toggle)
    var dirs_ok: Bool?
    var dropped: Int?
    var converting: Int?
    var untagged: Int?
    var review: Int?
    var review_items: [AudiobookDTO]?   // authoritative queue: review/ folder joined w/ ledger
    var organized_total: Int?
    var recent: [AudiobookDTO]?
    var library_visible: Bool?
    var error: String?
}

struct PlexSectionDTO: Codable, Identifiable, Hashable {
    var key: String?
    var title: String?
    var id: String { key ?? title ?? "" }
}

// MARK: - Unmatched (/api/unmatched — new endpoint)

struct UnmatchedTrackDTO: Codable, Identifiable, Hashable {
    var id: Int?
    var artist: String?
    var title: String?
    var album: String?
    var reason: String?
    var last_seen_at: String?
    var seen_count: Int?
    var uid: String { "\(id ?? 0)-\(title ?? "")" }
}

struct UnmatchedDTO: Codable {
    var rows: [UnmatchedTrackDTO]?
    var total: Int?
    var shown: Int?
    var limit: Int?
}

// MARK: - Suggestor (/api/unmatched/suggestions)

struct SuggestionAlbumDTO: Codable, Hashable {
    var album: String?
    var missing_count: Int?
    var needs_manual_count: Int?
    var coverage_gain_pct: Double?
}

struct UnmatchedSuggestionDTO: Codable, Identifiable, Hashable {
    var artist: String?
    var artist_key: String?
    var missing_count: Int?
    var needs_manual_count: Int?
    var coverage_gain_pct: Double?
    var missing_albums: [String]?
    var albums: [SuggestionAlbumDTO]?      // per-album breakdown, ranked by coverage gain
    var sample_titles: [String]?
    var id: String { artist_key ?? artist ?? "" }
}

struct UnmatchedSuggestionsDTO: Codable {
    var suggestions: [UnmatchedSuggestionDTO]?
    var current_coverage_pct: Int?
    var wanted_total: Int?
    var covered_total: Int?
}
