import SwiftUI

struct UnmatchedView: View {
    @EnvironmentObject var store: PlexifyStore
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .top) {
                    PageTitle(text: "Unmatched",
                              subtitle: "Liked tracks Plexify couldn't confidently match — logged for review.")
                    Spacer()
                    if !store.unmatched.isEmpty {
                        Button { Task { await store.clearUnmatched() } } label: { Text("Clear log") }
                            .buttonStyle(GhostButtonStyle(danger: true, small: true))
                    }
                }
                if store.unmatchedTotal > 0 {
                    Text("\(store.unmatchedTotal) unmatched · showing \(store.unmatched.count)")
                        .font(.system(size: 12)).foregroundStyle(PX.muted)
                }
            }.card()

            VStack(spacing: 0) {
                HStack {
                    Text("TITLE").frame(maxWidth: .infinity, alignment: .leading)
                    Text("ARTIST").frame(width: 200, alignment: .leading)
                    Text("REASON").frame(width: 200, alignment: .leading)
                    Text("SEEN").frame(width: 90, alignment: .trailing)
                }
                .font(.system(size: 11, weight: .semibold)).tracking(0.5).foregroundStyle(PX.muted)
                .padding(.vertical, 8).padding(.horizontal, 12)
                .overlay(alignment: .bottom) { Rectangle().fill(PX.lineStrong).frame(height: 1) }

                if store.unmatched.isEmpty {
                    Text("Nothing unmatched — every liked track found a home.")
                        .font(.system(size: 13)).foregroundStyle(PX.muted)
                        .frame(maxWidth: .infinity).padding(.vertical, 24)
                } else {
                    ForEach(store.unmatched) { r in
                        HStack {
                            Text(r.title ?? "?").foregroundStyle(PX.text).frame(maxWidth: .infinity, alignment: .leading).lineLimit(1)
                            Text(r.artist ?? "").foregroundStyle(PX.muted).frame(width: 200, alignment: .leading).lineLimit(1)
                            Text(r.reason ?? "").foregroundStyle(PX.muted).frame(width: 200, alignment: .leading).lineLimit(1)
                            Text(ago(r.last_seen_at)).foregroundStyle(PX.muted).frame(width: 90, alignment: .trailing)
                        }
                        .font(.system(size: 13.5)).padding(.vertical, 9).padding(.horizontal, 12)
                        .overlay(alignment: .bottom) { Rectangle().fill(PX.line).frame(height: 1) }
                    }
                }
            }.card(padding: 0)
        }
        .task { await store.loadUnmatched() }
    }
}
