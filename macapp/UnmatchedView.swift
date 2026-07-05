import SwiftUI

struct UnmatchedView: View {
    @EnvironmentObject var store: PlexifyStore

    private var maxMissing: Int { max(1, store.suggestions.map { $0.missing_count ?? 0 }.max() ?? 1) }

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            // Header — coverage summary
            VStack(alignment: .leading, spacing: 8) {
                PageTitle(text: "Unmatched",
                          subtitle: "Your biggest coverage gaps — which artists to get next, and the tracks the automated sources couldn't find.")
                if let info = store.suggestorInfo {
                    Text("\(info.covered_total ?? 0) of \(info.wanted_total ?? 0) wanted songs in Plex · \(info.current_coverage_pct ?? 0)% coverage")
                        .font(.system(size: 12)).foregroundStyle(PX.muted)
                }
            }.card()

            // Suggestions — the ranked "get these next" list
            VStack(alignment: .leading, spacing: 14) {
                HStack(spacing: 8) {
                    Text("Get these next").font(.system(size: 15, weight: .semibold)).foregroundStyle(PX.text)
                    if !store.suggestions.isEmpty { Badge(text: "\(store.suggestions.count)", tint: PX.plex) }
                    Spacer()
                }
                Text("Ranked by how much each artist would raise your coverage. The ones flagged “need manual” are what the sources gave up on — grab those and drop them in your import folder.")
                    .font(.system(size: 12)).foregroundStyle(PX.muted).fixedSize(horizontal: false, vertical: true)
                if store.suggestions.isEmpty {
                    Text("No gaps to suggest — either coverage is complete or the local mirror is still catching up.")
                        .font(.system(size: 13)).foregroundStyle(PX.muted)
                        .frame(maxWidth: .infinity).padding(.vertical, 18)
                } else {
                    ForEach(store.suggestions) { s in
                        ArtistSuggestionCard(s: s, maxMissing: maxMissing)
                    }
                }
            }.card()

            // The raw unmatched log (secondary)
            VStack(alignment: .leading, spacing: 0) {
                HStack {
                    Text("UNMATCHED LOG").font(.system(size: 11, weight: .semibold)).tracking(0.5).foregroundStyle(PX.muted)
                    Spacer()
                    if !store.unmatched.isEmpty {
                        Button { Task { await store.clearUnmatched() } } label: { Text("Clear log") }
                            .buttonStyle(GhostButtonStyle(danger: true, small: true))
                    }
                }
                .padding(.vertical, 8).padding(.horizontal, 12)
                .overlay(alignment: .bottom) { Rectangle().fill(PX.lineStrong).frame(height: 1) }

                if store.unmatched.isEmpty {
                    Text("Nothing in the log.").font(.system(size: 13)).foregroundStyle(PX.muted)
                        .frame(maxWidth: .infinity).padding(.vertical, 20)
                } else {
                    ForEach(store.unmatched.prefix(80)) { r in
                        HStack {
                            Text(r.title ?? "?").foregroundStyle(PX.text).frame(maxWidth: .infinity, alignment: .leading).lineLimit(1)
                            Text(r.artist ?? "").foregroundStyle(PX.muted).frame(width: 180, alignment: .leading).lineLimit(1)
                            Text(r.reason ?? "").foregroundStyle(PX.muted).frame(width: 180, alignment: .leading).lineLimit(1)
                            Text(ago(r.last_seen_at)).foregroundStyle(PX.muted).frame(width: 80, alignment: .trailing)
                        }
                        .font(.system(size: 13)).padding(.vertical, 8).padding(.horizontal, 12)
                        .overlay(alignment: .bottom) { Rectangle().fill(PX.line).frame(height: 1) }
                    }
                }
            }.card(padding: 0)
        }
        .task { await store.loadUnmatched(); await store.loadSuggestions() }
    }
}

private struct ArtistSuggestionCard: View {
    @EnvironmentObject var store: PlexifyStore
    let s: UnmatchedSuggestionDTO
    let maxMissing: Int
    @State private var busy = false
    @State private var expanded = false

    private var albums: [SuggestionAlbumDTO] { s.albums ?? [] }
    private var maxAlbumMissing: Int { max(1, albums.map { $0.missing_count ?? 0 }.max() ?? 1) }

    var body: some View {
        let missing = s.missing_count ?? 0
        let manual = s.needs_manual_count ?? 0
        HStack(alignment: .top, spacing: 16) {
            VStack(alignment: .leading, spacing: 8) {
                // Header — tap to expand the ranked album breakdown
                HStack(spacing: 8) {
                    if !albums.isEmpty {
                        Image(systemName: "chevron.right")
                            .font(.system(size: 10, weight: .bold)).foregroundStyle(PX.muted)
                            .rotationEffect(.degrees(expanded ? 90 : 0))
                    }
                    Text(s.artist ?? "?").font(.system(size: 15, weight: .semibold)).foregroundStyle(PX.text).lineLimit(1)
                    Badge(text: "+\(fmtGain(s.coverage_gain_pct))%", tint: PX.plex)
                    if manual > 0 { Badge(text: "\(manual) NEED MANUAL", tint: PX.warn) }
                    Spacer(minLength: 0)
                }
                .contentShape(Rectangle())
                .onTapGesture {
                    guard !albums.isEmpty else { return }
                    withAnimation(.easeInOut(duration: 0.16)) { expanded.toggle() }
                }
                Text("\(missing) missing track\(missing == 1 ? "" : "s")"
                     + (albums.isEmpty ? "" : " · \(albums.count) album\(albums.count == 1 ? "" : "s")"))
                    .font(.system(size: 12)).foregroundStyle(PX.text2)
                ProgressBarPX(fraction: Double(missing) / Double(max(1, maxMissing)), height: 6, solid: PX.plex)

                if expanded && !albums.isEmpty {
                    VStack(alignment: .leading, spacing: 0) {
                        Rectangle().fill(PX.line).frame(height: 1).padding(.top, 4).padding(.bottom, 4)
                        Text("ALBUMS BY COVERAGE GAIN").font(.system(size: 9, weight: .semibold))
                            .tracking(0.6).foregroundStyle(PX.muted).padding(.bottom, 2)
                        ForEach(albums, id: \.self) { alb in albumRow(alb) }
                    }
                    .transition(.opacity)
                } else if let names = s.missing_albums, !names.isEmpty {
                    Text(names.joined(separator: " · "))
                        .font(.system(size: 11)).foregroundStyle(PX.muted).lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            VStack(spacing: 8) {
                Button { act { await store.requeueSuggestion(s.artist_key ?? "") } } label: { Text("Re-try auto") }
                    .buttonStyle(GhostButtonStyle(small: true)).disabled(busy)
                Button { act { await store.dismissSuggestion(s.artist_key ?? "") } } label: { Text("Dismiss") }
                    .buttonStyle(GhostButtonStyle(danger: true, small: true)).disabled(busy)
            }
        }
        .inset(padding: 14, radius: PX.controlRadius, fill: PX.bg3, stroke: PX.line)
        .opacity(busy ? 0.5 : 1)
    }

    @ViewBuilder
    private func albumRow(_ alb: SuggestionAlbumDTO) -> some View {
        let n = alb.missing_count ?? 0
        let manual = alb.needs_manual_count ?? 0
        VStack(spacing: 3) {
            HStack(spacing: 8) {
                Text(alb.album ?? "?").font(.system(size: 12)).foregroundStyle(PX.text2).lineLimit(1)
                if manual > 0 {
                    Text("MANUAL").font(.system(size: 8, weight: .semibold)).tracking(0.4).foregroundStyle(PX.warn)
                        .padding(.horizontal, 4).padding(.vertical, 1)
                        .overlay(RoundedRectangle(cornerRadius: 3).stroke(PX.warn.opacity(0.5), lineWidth: 1))
                }
                Spacer(minLength: 8)
                Text("\(n) track\(n == 1 ? "" : "s")").font(.system(size: 11)).foregroundStyle(PX.muted)
                Text(gainLabel(alb.coverage_gain_pct)).font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(PX.plex).frame(width: 46, alignment: .trailing)
            }
            ProgressBarPX(fraction: Double(n) / Double(maxAlbumMissing), height: 3, solid: PX.plex.opacity(0.6))
        }
        .padding(.vertical, 5)
    }

    private func act(_ f: @escaping () async -> Void) { busy = true; Task { await f(); busy = false } }
    private func fmtGain(_ g: Double?) -> String {
        let v = g ?? 0
        return v >= 1 ? String(Int(v.rounded())) : String(format: "%.1f", v)
    }
    // Per-album gains are tiny (denominator ~2500); avoid a misleading "+0.0%" on 1-track albums.
    private func gainLabel(_ g: Double?) -> String {
        let v = g ?? 0
        if v <= 0 { return "—" }
        if v < 0.1 { return "<0.1%" }
        return "+\(fmtGain(v))%"
    }
}
