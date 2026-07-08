import SwiftUI

private enum LibTab: String, CaseIterable { case albums = "Albums", artists = "Artists", songs = "Songs" }
private enum LibFilter: String, CaseIterable { case all = "All", completed = "Completed", incomplete = "Incomplete", locked = "Locked" }
private struct LibSort: Identifiable { let key: String; let label: String; var id: String { key } }
private let libSorts = [
    LibSort(key: "recent", label: "Recently added"), LibSort(key: "album", label: "Title"),
    LibSort(key: "artist", label: "Artist"), LibSort(key: "largest", label: "Most tracks"),
    LibSort(key: "missing", label: "Fewest missing"),
]

struct LibraryView: View {
    @EnvironmentObject var store: PlexifyStore
    @State private var tab: LibTab = .albums
    @State private var filter: LibFilter = .all
    @State private var sort = "recent"
    @State private var query = ""
    @State private var selected: LibraryAlbumDTO?

    private let cols = [GridItem(.adaptive(minimum: 148, maximum: 210), spacing: 18)]

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            VStack(alignment: .leading, spacing: 8) {
                PageTitle(text: "Library",
                          subtitle: "Everything Plexify has placed in your Plex music library. Organize it like Plex, and condense it on demand.")
            }.card()

            if let album = selected {
                AlbumDetailCard(album: album) { selected = nil }
            } else {
                VStack(alignment: .leading, spacing: 14) {
                    toolbar
                    SearchField(placeholder: "Search…", text: $query)
                    grid
                }.card()
            }
        }
        .onChange(of: tab) { _, _ in Task { await reload() } }
        .onChange(of: filter) { _, _ in Task { await reload() } }
        .onChange(of: sort) { _, _ in Task { await reload() } }
        .onChange(of: query) { _, _ in Task { await reload() } }
        .task { await reload() }
    }

    private var toolbar: some View {
        HStack(spacing: 4) {
            DropMenu(current: filter.rawValue, options: LibFilter.allCases.map { $0.rawValue }) { sel in
                if let f = LibFilter(rawValue: sel) { filter = f }
            }
            DropMenu(current: tab.rawValue, options: LibTab.allCases.map { $0.rawValue }) { sel in
                if let t = LibTab(rawValue: sel) { tab = t }
            }
            DropMenu(current: libSorts.first { $0.key == sort }?.label ?? "Recently added",
                     options: libSorts.map { $0.label }) { sel in
                if let s = libSorts.first(where: { $0.label == sel }) { sort = s.key }
            }
            Text(countText).font(.system(size: 12)).foregroundStyle(PX.muted).padding(.leading, 8)
            Spacer()
            Button { Task { await store.condenseNow() } } label: {
                Label(store.condense?.state == "running"
                        ? (store.condense?.phase ?? "Condensing…")
                        : "Condense Library",
                      systemImage: "archivebox")
            }
            .buttonStyle(PrimaryButtonStyle())
            .disabled(store.condense?.state == "running")
            .help("Run the album rulebook now: merge duplicate album tiles, de-dupe tracks, and "
                  + "re-home mis-filed albums. Normally runs every 2h, but this Mac's scheduler is "
                  + "UI-only, so trigger it here.")
        }
    }

    private var countText: String {
        switch tab {
        case .albums: return store.albumsTotal > 0 ? "\(store.albumsTotal) albums" : ""
        case .artists: return store.artists.isEmpty ? "" : "\(store.artists.count) artists"
        case .songs: return store.songsTotal > 0 ? "\(store.songsTotal) songs" : ""
        }
    }

    @ViewBuilder private var grid: some View {
        switch tab {
        case .albums:
            if store.albums.isEmpty { EmptyLine() }
            else {
                LazyVGrid(columns: cols, alignment: .leading, spacing: 18) {
                    ForEach(store.albums) { a in
                        AlbumCard(album: a).onTapGesture { selected = a }
                    }
                }
                if store.albums.count < store.albumsTotal {
                    Button("Show more") { Task { await store.loadAlbums(q: query, filter: filter.rawValue.lowercased(), sort: sort, reset: false) } }
                        .buttonStyle(GhostButtonStyle()).frame(maxWidth: .infinity).padding(.top, 6)
                }
            }
        case .artists:
            if store.artists.isEmpty { EmptyLine() }
            else {
                LazyVGrid(columns: cols, alignment: .leading, spacing: 18) {
                    ForEach(store.artists) { ArtistCard(artist: $0) }
                }
            }
        case .songs:
            if store.songs.isEmpty { EmptyLine() } else { SongsTable(songs: store.songs) }
        }
    }

    private func reload() async {
        switch tab {
        case .albums: await store.loadAlbums(q: query, filter: filter.rawValue.lowercased(), sort: sort)
        case .artists: await store.loadArtists(q: query, filter: filter.rawValue.lowercased())
        case .songs: await store.loadSongs(q: query)
        }
    }
}

private struct EmptyLine: View {
    var body: some View {
        Text("Loading…").font(.system(size: 13)).foregroundStyle(PX.muted)
            .frame(maxWidth: .infinity).padding(.vertical, 24)
    }
}

// .lib-card — square-ish album tile: hue-gradient art + cover overlay, status outline, caption.
private struct AlbumCard: View {
    @EnvironmentObject var store: PlexifyStore
    let album: LibraryAlbumDTO
    @State private var hovering = false
    var body: some View {
        let completed = album.completed ?? false
        let outline: Color? = album.locked == true ? PX.plex : (completed ? PX.ok : (album.total_tracks != nil ? PX.warn : nil))
        VStack(alignment: .leading, spacing: 0) {
            ZStack(alignment: .topTrailing) {
                LibArt(hue: hueFor(album.artist, album.album), art: store.albumArtURL(album.id),
                       label: album.album ?? "?")
                if album.locked == true {
                    Badge(text: "Locked", tint: PX.plex).padding(6)
                } else if completed {
                    Image(systemName: "checkmark").font(.system(size: 11, weight: .heavy))
                        .foregroundStyle(.black).frame(width: 18, height: 18)
                        .background(Rectangle().fill(PX.ok)).padding(5)
                } else if let t = album.total_tracks {
                    Text("\(album.track_count ?? 0)/\(t)").font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(.white).padding(.horizontal, 6).padding(.vertical, 1)
                        .background(Rectangle().fill(Color.black.opacity(0.72))).padding(5)
                }
            }
            .overlay(outline.map { Rectangle().strokeBorder($0, lineWidth: 2).padding(0) })
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 4) {
                    if album.is_liked == true {
                        Image(systemName: "heart.fill").font(.system(size: 9)).foregroundStyle(PX.text2)
                    }
                    Text(album.album ?? "?").font(.system(size: 13, weight: .medium)).foregroundStyle(PX.text)
                        .lineLimit(1)
                }
                Text(album.artist ?? "?").font(.system(size: 11)).foregroundStyle(PX.muted).lineLimit(1)
            }.padding(.top, 8).padding(.horizontal, 2)
        }
        .offset(y: hovering ? -3 : 0)
        .animation(.easeOut(duration: 0.12), value: hovering)
        .onHover { hovering = $0 }
        .contentShape(Rectangle())
    }
}

private struct LibArt: View {
    let hue: Double
    let art: URL?
    let label: String
    var body: some View {
        let h = hue / 360.0
        ZStack(alignment: .bottomLeading) {
            LinearGradient(colors: [Color(hue: h, saturation: 0.58, brightness: 0.38),
                                    Color(hue: (h + 60/360).truncatingRemainder(dividingBy: 1), saturation: 0.58, brightness: 0.38)],
                           startPoint: .topLeading, endPoint: .bottomTrailing)
            Text(label).font(.system(size: 11, weight: .bold)).foregroundStyle(.white)
                .lineLimit(2).padding(8)
            if let art {
                AsyncImage(url: art) { img in img.resizable().aspectRatio(contentMode: .fill) }
                placeholder: { Color.clear }
            }
        }
        .aspectRatio(1, contentMode: .fit)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

private struct ArtistCard: View {
    @EnvironmentObject var store: PlexifyStore
    let artist: LibraryArtistDTO
    var body: some View {
        VStack(spacing: 8) {
            ZStack {
                LinearGradient(colors: [Color(hue: hueFor(artist.artist, nil)/360, saturation: 0.5, brightness: 0.35),
                                        Color(hue: (hueFor(artist.artist, nil)+60).truncatingRemainder(dividingBy: 360)/360, saturation: 0.5, brightness: 0.35)],
                               startPoint: .topLeading, endPoint: .bottomTrailing)
                if let url = store.albumArtURL(artist.id) {
                    AsyncImage(url: url) { img in img.resizable().aspectRatio(contentMode: .fill) } placeholder: { Color.clear }
                }
                Text(initials(artist.artist)).font(.system(size: 18, weight: .bold)).foregroundStyle(.white.opacity(0.9))
            }
            .aspectRatio(1, contentMode: .fit).clipShape(Circle())
            VStack(spacing: 1) {
                Text(artist.artist ?? "?").font(.system(size: 13, weight: .medium)).foregroundStyle(PX.text).lineLimit(1)
                Text("\(artist.albums ?? 0) albums").font(.system(size: 11)).foregroundStyle(PX.muted)
            }
        }
    }
}

// .lib-songtable
private struct SongsTable: View {
    let songs: [LibrarySongDTO]
    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("TITLE").frame(maxWidth: .infinity, alignment: .leading)
                Text("ARTIST").frame(width: 200, alignment: .leading)
                Text("ALBUM").frame(width: 180, alignment: .leading)
                Text("STATE").frame(width: 110, alignment: .trailing)
            }
            .font(.system(size: 11, weight: .semibold)).tracking(0.5).foregroundStyle(PX.muted)
            .padding(.vertical, 8).padding(.horizontal, 12)
            .overlay(alignment: .bottom) { Rectangle().fill(PX.lineStrong).frame(height: 1) }
            ForEach(songs) { s in
                HStack {
                    Text(s.title ?? "?").foregroundStyle(PX.text).frame(maxWidth: .infinity, alignment: .leading).lineLimit(1)
                    Text(s.artist ?? "").foregroundStyle(PX.muted).frame(width: 200, alignment: .leading).lineLimit(1)
                    Text(s.album ?? "").foregroundStyle(PX.muted).frame(width: 180, alignment: .leading).lineLimit(1)
                    Text(s.on_disk == false ? "missing" : "on disk")
                        .foregroundStyle(s.on_disk == false ? PX.muted : PX.ok)
                        .frame(width: 110, alignment: .trailing)
                }
                .font(.system(size: 13.5)).padding(.vertical, 9).padding(.horizontal, 12)
                .overlay(alignment: .bottom) { Rectangle().fill(PX.line).frame(height: 1) }
                .opacity(s.on_disk == false ? 0.55 : 1)
            }
        }
    }
}

// .lib-detail — album drill-down: cover + meta + tracklist.
private struct AlbumDetailCard: View {
    @EnvironmentObject var store: PlexifyStore
    let album: LibraryAlbumDTO
    let onBack: () -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Button { onBack() } label: { Label("Back", systemImage: "chevron.left") }
                .buttonStyle(GhostButtonStyle(small: true))
            HStack(alignment: .bottom, spacing: 22) {
                LibArt(hue: hueFor(album.artist, album.album), art: store.albumArtURL(album.id), label: album.album ?? "?")
                    .frame(width: 170, height: 170)
                VStack(alignment: .leading, spacing: 6) {
                    Text(album.artist ?? "?").font(.system(size: 13)).foregroundStyle(PX.muted)
                    Text(album.album ?? "?").font(.system(size: 26, weight: .semibold)).tracking(-0.3)
                        .foregroundStyle(PX.text)
                    let n = store.albumDetail?.tracks?.count ?? album.track_count ?? 0
                    Text("\(n) " + (n == 1 ? "track" : "tracks")
                         + ((album.total_tracks != nil) ? " of \(album.total_tracks!)" : ""))
                        .font(.system(size: 13)).foregroundStyle(PX.muted)
                    if let id = album.id {
                        Button { store.openAlbumInPlex(id) } label: {
                            Text("Open in Plex →").font(.system(size: 13)).foregroundStyle(PX.plex)
                        }.buttonStyle(.plain)
                    }
                }
                Spacer()
            }
            // tracklist
            VStack(spacing: 0) {
                ForEach(Array((store.albumDetail?.tracks ?? []).enumerated()), id: \.offset) { i, t in
                    HStack(spacing: 14) {
                        Text("\(t.track_no ?? (i + 1))").font(.system(size: 12)).monospacedDigit()
                            .foregroundStyle(PX.muted).frame(width: 24, alignment: .trailing)
                        Text(t.title ?? "?").font(.system(size: 14)).foregroundStyle(PX.text)
                            .frame(maxWidth: .infinity, alignment: .leading).lineLimit(1)
                        if let dur = t.duration, dur > 0 {
                            Text(fmtDuration(dur)).font(.system(size: 12)).monospacedDigit().foregroundStyle(PX.muted)
                        }
                    }
                    .padding(.vertical, 10).padding(.horizontal, 12)
                    .overlay(alignment: .bottom) { Rectangle().fill(PX.line).frame(height: 1) }
                }
            }
            .overlay(alignment: .top) { Rectangle().fill(PX.line).frame(height: 1) }
        }
        .card()
        .task { if let id = album.id { await store.loadAlbumDetail(id) } }
    }
    func fmtDuration(_ v: Int) -> String {
        let secs = v > 10000 ? v / 1000 : v          // ms vs s tolerance
        return String(format: "%d:%02d", secs / 60, secs % 60)
    }
}
