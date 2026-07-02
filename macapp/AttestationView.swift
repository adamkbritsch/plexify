import SwiftUI

// Full-screen legal-use gate. Shown by PlexifyRootView whenever the engine reports the user
// hasn't attested — the entire app (nav + all pages) is unreachable until they agree. The
// authoritative enforcement is engine-side (nas_downloader), so this is the UX surface, not
// the security boundary.
struct AttestationView: View {
    @EnvironmentObject var store: PlexifyStore
    @State private var agreed = false
    @State private var submitting = false

    var body: some View {
        ZStack {
            PX.bg.ignoresSafeArea()
            if store.attestation == nil {
                ProgressView().controlSize(.large).tint(PX.plex)   // still checking
            } else {
                VStack(spacing: 22) {
                    Wordmark()
                    VStack(alignment: .leading, spacing: 16) {
                        Text("Before you start")
                            .font(.system(size: 20, weight: .semibold)).foregroundStyle(PX.text)
                        Text("Plexify can acquire music from peer-to-peer and mirror sources. Before it downloads anything, please confirm you'll use it legally.")
                            .font(.system(size: 14)).foregroundStyle(PX.text2)
                            .fixedSize(horizontal: false, vertical: true)
                        Toggle(isOn: $agreed) {
                            Text("I confirm that I own, or otherwise have the legal right to, the music I download with Plexify, and that I will use it in compliance with applicable law and the terms of each service I connect.")
                                .font(.system(size: 13)).foregroundStyle(PX.text)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        .toggleStyle(.switch).tint(PX.plex)
                        Text("Plexify is a personal library tool — you are responsible for how you use it. Downloading stays blocked until you agree.")
                            .font(.system(size: 12)).foregroundStyle(PX.muted)
                            .fixedSize(horizontal: false, vertical: true)
                        HStack {
                            Spacer()
                            Button {
                                submitting = true
                                Task { _ = await store.submitAttest(); submitting = false }
                            } label: { Text(submitting ? "Saving…" : "I agree & continue") }
                                .buttonStyle(PrimaryButtonStyle())
                                .disabled(!agreed || submitting)
                                .opacity(agreed && !submitting ? 1 : 0.5)
                        }
                    }
                    .card()
                    .frame(maxWidth: 560)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .padding(40)
            }
        }
        .environment(\.colorScheme, .dark)
        .task { await store.loadAttestStatus() }
    }
}
