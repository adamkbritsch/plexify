import SwiftUI

struct DashboardView: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            PipelineStripCard()
            ControlBarCard()
            SourcesHealthCard()
            RightNowCard()
            RecentlyAddedCard()
        }
    }
}

// MARK: - Split pipeline (NAS downloader → Staging → Mac organizer → Plex)
// Makes the two-machine architecture legible: the NAS downloads autonomously and dumps to
// staging; this Mac organizes staging into the Plex library. Un-organized staging never
// appears in the library — it lives here as "ready to organize".

// A slim single-line status bar (not a big card of boxes) — reads like a breadcrumb of the
// split's state without competing with the controls below it.
private struct PipelineStripCard: View {
    @EnvironmentObject var store: PlexifyStore
    var body: some View {
        let nas = store.nas
        let p = store.picker
        let reachable = nas?.reachable ?? false
        let running = nas?.running ?? 0
        let ready = nas?.ready ?? 0
        let importPending = nas?.import_pending ?? 0
        let staged = ready + importPending
        let paused = p?.paused ?? true
        HStack(spacing: 12) {
            PipeSeg(dot: reachable ? PX.ok : PX.danger, name: "NAS",
                    value: !reachable ? "Unreachable" : (running > 0 ? "\(running) downloading" : "Idle"),
                    help: reachable ? "NAS downloader (autonomous) — \(nas?.queued ?? 0) queued"
                                    : "NAS downloader unreachable — check Tailscale / NAS")
            PipeSep()
            PipeSeg(dot: staged > 0 ? PX.warn : PX.muted, name: "Staging",
                    value: "\(staged) ready",
                    help: importPending > 0
                        ? "\(importPending) dropped in the import folder + \(ready) downloaded — awaiting organization (resume the picker to import)"
                        : "Downloaded, awaiting organization into the library")
            PipeSep()
            PipeSeg(dot: paused ? PX.warn : PX.ok, name: "Organizer",
                    value: paused ? "Paused" : "Active",
                    help: "This Mac — \(p?.queue_depth ?? 0) to acquire · \(p?.in_flight ?? 0) working")
            PipeSep()
            PipeSeg(dot: PX.ok, name: "Plex",
                    value: p?.plex_coverage_pct.map { "\($0)%" } ?? "—", help: "Plex match coverage")
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 16).padding(.vertical, 10)
        .background(Rectangle().fill(PX.bg2))
        .overlay(Rectangle().strokeBorder(PX.line, lineWidth: 1))
    }
}

private struct PipeSeg: View {
    let dot: Color; let name: String; let value: String; var help: String = ""
    var body: some View {
        HStack(spacing: 7) {
            Dot(color: dot, size: 7)
            Text(name.uppercased()).font(.system(size: 10.5, weight: .semibold)).tracking(0.5)
                .foregroundStyle(PX.muted)
            Text(value).font(.system(size: 12.5, weight: .semibold)).foregroundStyle(PX.text)
        }
        .help(help)
    }
}

private struct PipeSep: View {
    var body: some View {
        Image(systemName: "chevron.right").font(.system(size: 9, weight: .semibold))
            .foregroundStyle(PX.muted.opacity(0.6))
    }
}

// MARK: - Control bar (.card.dash-controls)

private struct ControlBarCard: View {
    @EnvironmentObject var store: PlexifyStore
    // "lanes / ceiling" (AIMD auto-tuned) — matches the web's "auto 5 / 10".
    var concurrencyText: String {
        guard let c = store.picker?.concurrency_ceiling else { return "—" }
        let lanes = store.picker?.max_instances ?? c
        return "\(lanes) / \(c)"
    }
    var body: some View {
        let paused = store.picker?.paused ?? false
        VStack(alignment: .leading, spacing: 10) {
            // main action row
            HStack(spacing: 10) {
                Button { Task { await store.refreshAll() } } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }.buttonStyle(DashCtlButtonStyle())
                Button { Task { await store.runPickerNow() } } label: {
                    Label("Run picker now", systemImage: "bolt.fill")
                }.buttonStyle(DashCtlButtonStyle())
                Button { Task { await store.setPickerPaused(!paused) } } label: {
                    Label(paused ? "Resume picker" : "Pause picker",
                          systemImage: paused ? "play.fill" : "pause.fill")
                }.buttonStyle(DashCtlButtonStyle(active: paused))
                FillControl()
                Spacer(minLength: 8)
                Button { store.reconnectSpotify() } label: {
                    Label("Reconnect Spotify", systemImage: "link")
                }.buttonStyle(DashCtlButtonStyle())
                AutoRefreshToggle()
                RefreshStatus()
            }
            Rectangle().fill(Color.white.opacity(0.06)).frame(height: 1).padding(.top, 2)
            // stats sub-row
            HStack(spacing: 18) {
                CtlStat("In flight", "\(store.picker?.in_flight ?? 0)")
                CtlStat("Queue", "\(store.picker?.queue_depth ?? store.live?.queue_depth ?? 0)")
                CtlStat("Next tick", fmtSecs(store.picker?.next_run_in_seconds))
                CtlStat("Concurrency", concurrencyText, badge: "AUTO")
                CtlStat("Plex coverage", store.picker?.plex_coverage_pct.map { "\($0)%" } ?? "—")
                Spacer()
            }
        }
        .card(padding: 16)
    }
}

private struct CtlStat: View {
    let label: String
    let value: String
    var badge: String? = nil
    init(_ label: String, _ value: String, badge: String? = nil) {
        self.label = label; self.value = value; self.badge = badge
    }
    var body: some View {
        HStack(spacing: 6) {
            HStack(spacing: 5) {
                Text(label.uppercased()).font(.system(size: 11)).tracking(0.4)
                    .foregroundStyle(PX.muted)
                if let badge {
                    Text(badge).font(.system(size: 8, weight: .bold)).tracking(0.5)
                        .foregroundStyle(PX.sp)
                        .padding(.horizontal, 4).padding(.vertical, 1)
                        .background(Capsule().fill(PX.sp.opacity(0.14)))
                }
            }
            Text(value).font(.system(size: 13, weight: .semibold)).monospacedDigit()
                .foregroundStyle(PX.text)
        }
    }
}

private struct AutoRefreshToggle: View {
    @EnvironmentObject var store: PlexifyStore
    var body: some View {
        Button { store.autoRefresh.toggle() } label: {
            HStack(spacing: 6) {
                Image(systemName: store.autoRefresh ? "checkmark.square.fill" : "square")
                    .font(.system(size: 12))
                    .foregroundStyle(store.autoRefresh ? PX.plex : PX.muted)
                Text("Auto-refresh").font(.system(size: 13)).foregroundStyle(PX.text2)
            }
        }.buttonStyle(.plain)
    }
}

private struct RefreshStatus: View {
    @EnvironmentObject var store: PlexifyStore
    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { _ in
            Text(statusText)
                .font(.system(size: 12)).monospacedDigit()
                .foregroundStyle(PX.muted)
                .frame(minWidth: 62, alignment: .trailing)
        }
    }
    var statusText: String {
        guard store.autoRefresh else { return "paused" }
        guard let t = store.lastRefresh else { return "…" }
        let s = Int(Date().timeIntervalSince(t))
        return s < 3 ? "just now" : "\(s)s ago"
    }
}

// Fill balance — a dropdown matching the web's popover options (Auto + 5 manual levels).
private struct FillControl: View {
    @EnvironmentObject var store: PlexifyStore
    private let labels = ["All albums", "Mostly albums", "Even", "Mostly new", "All new"]
    var body: some View {
        DropMenu(leading: "Fill", current: fillLabel, options: ["Auto"] + labels, compact: true) { sel in
            Task {
                if sel == "Auto" { await store.setFill(mode: "auto", value: nil) }
                else if let i = labels.firstIndex(of: sel) { await store.setFill(mode: "manual", value: i) }
            }
        }
        .task { await store.loadFill() }
    }
    var fillLabel: String {
        guard let f = store.fill else { return "Even" }
        if (f.mode ?? "auto") == "auto" {
            if let a = f.fill_per_4, let b = f.acquire_per_4 { return "Auto \(a)·\(b)" }
            return "Auto"
        }
        return labels[min(max(f.value ?? 2, 0), 4)]
    }
}

// MARK: - Sources health (.card.sources-health)

private struct SourcesHealthCard: View {
    @EnvironmentObject var store: PlexifyStore
    // web order: soulseek, squid, spotiflac, telegram
    private let order: [(key: String, fallback: String)] = [
        ("soulseek", "Soulseek"), ("squid", "squid.wtf"),
        ("spotiflac", "SpotiFLAC"), ("telegram", "Telegram")
    ]
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            CardLabel(text: "Download sources · NAS")
            HStack(spacing: 10) {
                ForEach(order, id: \.key) { entry in
                    let svc = store.health?.services?[entry.key]
                    SourceHealthPill(
                        name: svc?.name ?? entry.fallback,
                        detail: svc?.detail ?? "…",
                        status: svc?.state
                    )
                }
            }
        }
        .card(padding: 16)
    }
}

// MARK: - Right now (.card #live-card)

private struct RightNowCard: View {
    @EnvironmentObject var store: PlexifyStore
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            CardLabel(text: "Right now")
            let dls = store.live?.downloads ?? (store.live?.downloading.map { [$0] } ?? [])
            if dls.isEmpty {
                LiveIdle()
            } else {
                VStack(spacing: 14) {
                    ForEach(dls) { LiveStrip(d: $0) }
                }
            }
        }
        .card()
    }
}

private struct LiveIdle: View {
    @EnvironmentObject var store: PlexifyStore
    var body: some View {
        let qd = store.live?.queue_depth ?? 0
        let nx = store.live?.next_picker_tick_in_seconds
        var parts = ["Queue idle"]
        if let nx { parts.append("next picker tick in \(nx)s") }
        parts.append(qd > 0 ? "\(qd) albums waiting" : "all caught up")
        return HStack(spacing: 10) {
            Dot(color: PX.muted, size: 8)
            Text(parts.joined(separator: " · ")).font(.system(size: 13)).foregroundStyle(PX.muted)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 4)
    }
}

private struct LiveStrip: View {
    @EnvironmentObject var store: PlexifyStore
    let d: AcquisitionDTO
    var body: some View {
        let done = d.tracks_done ?? 0, total = d.tracks_total ?? 0
        let frac = total > 0 ? Double(done) / Double(total) : 0
        HStack(alignment: .top, spacing: 16) {
            // art (56x56, green, initials fallback)
            ZStack(alignment: .bottomLeading) {
                RoundedRectangle(cornerRadius: 6).fill(PX.sp)
                if let url = store.albumArtURL(d.row_id) {
                    AsyncImage(url: url) { img in
                        img.resizable().aspectRatio(contentMode: .fill)
                    } placeholder: { Color.clear }
                    .frame(width: 56, height: 56).clipShape(RoundedRectangle(cornerRadius: 6))
                }
                Text(String((d.artist ?? "?").prefix(4)))
                    .font(.system(size: 10, weight: .semibold)).foregroundStyle(.white.opacity(0.85))
                    .padding(6)
            }
            .frame(width: 56, height: 56)

            VStack(alignment: .leading, spacing: 3) {
                // title + chips
                HStack(spacing: 8) {
                    Text(titleText).font(.system(size: 15, weight: .semibold)).foregroundStyle(PX.text)
                        .lineLimit(1)
                    if let q = qualityChip { QualityChip(label: q.0, hires: q.1) }
                    if let s = d.source, !s.isEmpty { SourceTag(source: sourceDisplay(s)) }
                    if d.upgrading == true {
                        Label("Upgrading", systemImage: "arrow.up")
                            .font(.system(size: 10, weight: .semibold)).foregroundStyle(PX.plex)
                    }
                }
                if let songs = d.song_names, !songs.isEmpty {
                    HStack(spacing: 5) {
                        Image(systemName: "music.note").font(.system(size: 10)).foregroundStyle(PX.muted)
                        Text(songs.prefix(6).joined(separator: ", ")
                             + (songs.count > 6 ? "  +\(songs.count - 6) more" : ""))
                            .font(.system(size: 11)).foregroundStyle(PX.text2).lineLimit(1)
                    }
                }
                Text(subText).font(.system(size: 11)).foregroundStyle(PX.muted)
                ProgressBarPX(fraction: frac, height: 6, solid: PX.sp).padding(.top, 3)
            }
        }
    }
    var titleText: String {
        [d.artist, d.album].compactMap { $0?.isEmpty == false ? $0 : nil }.joined(separator: " — ")
            .ifEmpty("Downloading…")
    }
    var subText: String {
        var parts = ["row #\(d.row_id ?? 0)"]
        let total = d.tracks_total ?? 0
        if total > 0 { parts.append("\(d.tracks_done ?? 0) / \(total) tracks") }
        if let e = d.elapsed_seconds { parts.append("\(fmtSecs(e)) elapsed") }
        if let eta = d.eta_seconds { parts.append("~\(fmtSecs(eta)) left") }
        return parts.joined(separator: " · ")
    }
    var qualityChip: (String, Bool)? {
        switch (d.quality_acquired ?? d.quality_target) {
        case "HI_RES_LOSSLESS": return ("Hi-Res", true)
        case "LOSSLESS":        return ("CD", false)
        default: return nil
        }
    }
}

// MARK: - Recently added (.card #feed-card)

private struct RecentlyAddedCard: View {
    @EnvironmentObject var store: PlexifyStore
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            CardLabel(text: "Recently added").padding(.bottom, 4)
            if store.reward.isEmpty {
                Text("Nothing imported yet. Library autofill is running — check back in a few minutes.")
                    .font(.system(size: 13)).foregroundStyle(PX.muted)
                    .frame(maxWidth: .infinity).padding(.vertical, 20)
            } else {
                ForEach(store.reward) { FeedRow(item: $0) }
                if store.rewardHasMore {
                    Button { Task { await store.loadMoreReward() } } label: {
                        Text("Show 10 more →").font(.system(size: 13)).foregroundStyle(PX.plex)
                            .frame(maxWidth: .infinity).padding(8)
                    }.buttonStyle(.plain).padding(.top, 6)
                }
            }
        }
        .card()
        .task { if store.reward.isEmpty { await store.refreshRewardHead() } }
    }
}

private struct FeedRow: View {
    @EnvironmentObject var store: PlexifyStore
    let item: RewardItemDTO
    @State private var hovering = false
    var body: some View {
        HStack(spacing: 14) {
            FeedArt(item: item, art: store.albumArtURL(item.id_))
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 5) {
                    if item.liked == true || item.is_liked == true {
                        Image(systemName: "heart.fill").font(.system(size: 10)).foregroundStyle(PX.sp)
                    }
                    Text(item.song ?? item.name ?? "?").font(.system(size: 14)).foregroundStyle(PX.text)
                        .lineLimit(1)
                }
                Text("\((item.artist ?? "?"))  ·  \((item.album ?? "?"))")
                    .font(.system(size: 11)).foregroundStyle(PX.muted).lineLimit(1)
                HStack(spacing: 8) {
                    if let q = feedQuality { FeedQualityTag(text: q.0, tint: q.1) }
                    if item.was_upgraded == true {
                        Label("Upgraded", systemImage: "arrow.up")
                            .font(.system(size: 10)).foregroundStyle(PX.plex)
                    }
                    if let sz = sizeStr { Text(sz).font(.system(size: 10)).foregroundStyle(PX.muted) }
                }
            }
            Spacer(minLength: 8)
            if let src = item.source, !src.isEmpty { SourceTag(source: sourceDisplay(src)) }
            TimelineView(.periodic(from: .now, by: 20)) { _ in
                Text(ago(item.imported_at)).font(.system(size: 11)).foregroundStyle(PX.muted)
                    .frame(width: 72, alignment: .trailing)
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 14)
        .background(RoundedRectangle(cornerRadius: 6).fill(hovering ? Color.white.opacity(0.02) : .clear))
        .overlay(alignment: .bottom) { Rectangle().fill(Color.white.opacity(0.04)).frame(height: 1).padding(.horizontal, 14) }
        .contentShape(Rectangle())
        .onHover { hovering = $0 }
        .onTapGesture { if let id = item.id_ { store.openAlbumInPlex(id) } }
    }
    var sizeStr: String? { let s = fmtBytes(item.size_bytes); return s.isEmpty ? nil : s }
    // Quality is meaningful info → keeps color: hi-res gold, CD/lossless neutral, lossy red.
    var feedQuality: (String, Color)? {
        let label = item.quality_label ?? ""
        switch item.quality_tier {
        case "hires":    return ("Hi-Res \(label)", PX.plex)
        case "cd":       return ("FLAC \(label)", PX.text2)
        case "lossless": return (item.codec ?? "Lossless", PX.text2)
        case "lossy":    return (item.codec ?? "Lossy", PX.danger)
        default: return nil
        }
    }
}

private struct FeedQualityTag: View {
    let text: String; let tint: Color
    var body: some View {
        Text(text).font(.system(size: 10, weight: .semibold)).foregroundStyle(tint)
            .padding(.horizontal, 7).padding(.vertical, 1)
            .background(RoundedRectangle(cornerRadius: PX.chipRadius)
                .fill(tint == PX.plex ? PX.plex.opacity(0.16) : Color.white.opacity(0.06)))
            .overlay(RoundedRectangle(cornerRadius: PX.chipRadius)
                .strokeBorder(tint == PX.plex ? PX.plex.opacity(0.40) : Color.white.opacity(0.12), lineWidth: 1))
    }
}

// .feed-art — 48x48 hue-derived gradient with album art overlay + initials fallback.
private struct FeedArt: View {
    let item: RewardItemDTO
    let art: URL?
    var body: some View {
        let hue = hueFor(item.artist, item.album) / 360.0
        ZStack {
            LinearGradient(
                colors: [Color(hue: hue, saturation: 0.7, brightness: 0.5),
                         Color(hue: (hue + 60/360).truncatingRemainder(dividingBy: 1),
                               saturation: 0.7, brightness: 0.5)],
                startPoint: .topLeading, endPoint: .bottomTrailing)
            Text(initials(item.artist))
                .font(.system(size: 9, weight: .semibold)).foregroundStyle(.white.opacity(0.85))
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottomLeading).padding(5)
            if let art {
                AsyncImage(url: art) { img in img.resizable().aspectRatio(contentMode: .fill) }
                placeholder: { Color.clear }
            }
        }
        .frame(width: 48, height: 48).clipShape(RoundedRectangle(cornerRadius: 5))
    }
}

extension String {
    func ifEmpty(_ fallback: String) -> String { isEmpty ? fallback : self }
}
