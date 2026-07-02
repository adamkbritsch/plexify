import SwiftUI

struct PlaylistsView: View {
    @EnvironmentObject var store: PlexifyStore
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            // source banner (.source-banner — green left rule)
            HStack(spacing: 14) {
                Dot(color: PX.sp, size: 10)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Spotify source").font(.system(size: 11, weight: .semibold)).tracking(0.6)
                        .foregroundStyle(PX.muted)
                    Text(store.playlistsSource ?? "Connected").font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(PX.text)
                }
                Spacer()
                Button { Task { await store.syncAll() } } label: { Label("Sync all", systemImage: "arrow.triangle.2.circlepath") }
                    .buttonStyle(PrimaryButtonStyle())
            }
            .padding(.horizontal, 16).padding(.vertical, 12)
            .overlay(alignment: .leading) { Rectangle().fill(PX.sp).frame(width: 3) }
            .background(Rectangle().fill(PX.bg2))
            .overlay(Rectangle().strokeBorder(PX.line, lineWidth: 1))

            VStack(alignment: .leading, spacing: 8) {
                if store.playlists.isEmpty {
                    Text("No mirrored playlists yet.").font(.system(size: 13)).foregroundStyle(PX.muted)
                        .frame(maxWidth: .infinity).padding(.vertical, 20)
                } else {
                    ForEach(store.playlists) { PlaylistRow(pair: $0) }
                }
            }
        }
        .task { await store.loadPlaylists() }
    }
}

// .playlist-row
private struct PlaylistRow: View {
    @EnvironmentObject var store: PlexifyStore
    let pair: PlaylistPairDTO
    @State private var hovering = false
    var body: some View {
        let mirrored = pair.mirrored ?? false
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 10) {
                    Text(pair.name ?? pair.spotify_name ?? "Playlist").font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(PX.text)
                    if mirrored { Badge(text: "Mirrored", tint: PX.ok) }
                    if let st = pair.status, !st.isEmpty, !mirrored { Badge(text: st, tint: statusBadgeTint(st)) }
                }
                HStack(spacing: 8) {
                    if let n = pair.track_count { Text("\(n) tracks").font(.system(size: 12)).foregroundStyle(PX.muted) }
                    if let ip = pair.in_plex, let n = pair.track_count {
                        Text("· \(ip)/\(n) in Plex").font(.system(size: 12)).foregroundStyle(PX.muted)
                    }
                    if let ls = pair.last_synced_at { Text("· synced \(ago(ls))").font(.system(size: 12)).foregroundStyle(PX.muted) }
                }
            }
            Spacer()
            if let id = pair.id {
                Button { Task { await store.syncPair(id) } } label: { Text("Sync") }
                    .buttonStyle(GhostButtonStyle(small: true))
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 12)
        .background(Rectangle().fill(hovering ? Color.white.opacity(0.025) : PX.bg2))
        .overlay(Rectangle().strokeBorder(mirrored ? PX.sp.opacity(0.3) : PX.line, lineWidth: 1))
        .overlay(alignment: .leading) { mirrored ? Rectangle().fill(PX.sp).frame(width: 2) : nil }
        .onHover { hovering = $0 }
    }
}
