import SwiftUI
import AppKit

enum Page: String, CaseIterable, Identifiable {
    case dashboard = "Dashboard"
    case playlists = "Playlists"
    case library   = "Library"
    case settings  = "Settings"
    case jobs      = "Jobs"
    case unmatched = "Unmatched"
    var id: String { rawValue }
    var primary: Bool { self != .unmatched }   // Unmatched is de-emphasized in the web nav
}

// The Plexify wordmark — renders the bundled logo-plexify.svg (white wordmark), falling
// back to styled text if the asset is missing.
struct Wordmark: View {
    var body: some View {
        if let url = Bundle.main.url(forResource: "logo-plexify", withExtension: "svg"),
           let img = NSImage(contentsOf: url) {
            Image(nsImage: img).resizable().aspectRatio(contentMode: .fit).frame(height: 34)  // matches web .brand-logo-wordmark
                .accessibilityLabel("Plexify")
        } else {
            Text("Plexify").font(.system(size: 26, weight: .bold)).foregroundStyle(PX.text)
        }
    }
}

// .topbar — true-black header, hairline underneath: wordmark + square status mark on the
// left, nav tabs on the right (gold hover + animated underline). No blur, no translucency.
struct TopBar: View {
    @EnvironmentObject var store: PlexifyStore
    @Binding var page: Page
    var body: some View {
        HStack(spacing: 10) {
            Button { page = .dashboard } label: {
                HStack(spacing: 10) {
                    Wordmark()
                    StatusDot(overall: store.health?.overall)
                }
            }.buttonStyle(.plain)
            Spacer()
            HStack(spacing: 28) {
                ForEach(Page.allCases) { p in
                    NavTab(title: p.rawValue, active: page == p, secondary: !p.primary) { page = p }
                }
            }
        }
        .padding(.horizontal, 24).padding(.vertical, 16)
        .frame(minHeight: 64)
        .background(PX.bg)
        .overlay(alignment: .bottom) { Rectangle().fill(PX.line).frame(height: 1) }
    }
}

struct StatusDot: View {
    let overall: String?
    var body: some View {
        Dot(color: healthColor(overall), size: 10)
            .help("System status: \(overall ?? "unknown")")
    }
}

struct NavTab: View {
    let title: String
    let active: Bool
    var secondary: Bool = false
    let action: () -> Void
    @State private var hovering = false
    var body: some View {
        Button(action: action) {
            Text(title)
                .font(.system(size: secondary ? 13.5 : 15.5, weight: .medium))
                .foregroundStyle(active || hovering ? PX.plex : PX.text)
                .opacity(secondary && !active ? 0.7 : 1)
                .overlay(alignment: .bottom) {
                    Rectangle().fill(PX.plex)
                        .frame(height: 2)
                        .scaleEffect(x: (active || hovering) ? 1 : 0, anchor: .leading)
                        .offset(y: 6)
                        .animation(.easeOut(duration: 0.2), value: hovering)
                }
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
    }
}

// Root: sticky top bar + a scrolling, max-width-1240, centered content column over the
// radial body background. Switches the page view on the nav selection.
struct PlexifyRootView: View {
    @EnvironmentObject var store: PlexifyStore
    @State private var page: Page = .dashboard
    var body: some View {
        PX.bg.ignoresSafeArea()
            .overlay {
                if store.attestation?.attested == true {
                    VStack(spacing: 0) {
                        TopBar(page: $page)
                        ScrollView {
                            Group {
                                switch page {
                                case .dashboard: DashboardView()
                                case .playlists: PlaylistsView()
                                case .library:   LibraryView()
                                case .settings:  SettingsView()
                                case .jobs:      JobsView()
                                case .unmatched: UnmatchedView()
                                }
                            }
                            .padding(24)
                            .frame(maxWidth: PX.contentMaxWidth)
                            .frame(maxWidth: .infinity, alignment: .top)
                        }
                    }
                } else {
                    // Legal-use gate blocks the entire app until the user agrees. `nil` =
                    // still checking → the gate view shows a brief loading state.
                    AttestationView()
                }
            }
            .environment(\.colorScheme, .dark)
            .foregroundStyle(PX.text)
            .onChange(of: page) { _, newValue in
                store.dashboardVisible = (newValue == .dashboard)
                Task { await onEnter(newValue) }
            }
            .task { await onEnter(.dashboard) }
    }

    // Load a page's data when it becomes visible.
    func onEnter(_ p: Page) async {
        switch p {
        case .dashboard: await store.refreshAll()
        case .library:   if store.albums.isEmpty { await store.loadAlbums() }
        case .jobs:      await store.loadJobs()
        case .playlists: await store.loadPlaylists()
        case .settings:  await store.loadSettings()
        case .unmatched: await store.loadUnmatched()
        }
    }
}
