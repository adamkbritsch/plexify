import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var store: PlexifyStore
    // local edit state, seeded from the loaded config
    @State private var enabled = false
    @State private var interval = 30
    @State private var quality = "LOSSLESS"
    @State private var hiresOnly = false
    @State private var pauseStreaming = true
    @State private var autostarEnabled = false
    @State private var autostarDryRun = false
    @State private var loaded = false

    private let qualities = ["LOSSLESS", "HI_RES_LOSSLESS"]

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            VStack(alignment: .leading, spacing: 8) {
                PageTitle(text: "Settings", subtitle: "Autofill, download sources, and connected services.")
            }.card()

            // Autofill
            VStack(alignment: .leading, spacing: 14) {
                CardLabel(text: "Library autofill")
                Toggle(isOn: $enabled) { Text("Enable autofill").font(.system(size: 14)).foregroundStyle(PX.text) }
                    .toggleStyle(.switch).tint(PX.plex)
                HStack(spacing: 12) {
                    Text("Interval").font(.system(size: 13)).foregroundStyle(PX.text2)
                    Stepper(value: $interval, in: 5...360, step: 5) {
                        Text("\(interval) min").font(.system(size: 13)).monospacedDigit().foregroundStyle(PX.text)
                    }
                }
                HStack(spacing: 12) {
                    Text("Quality target").font(.system(size: 13)).foregroundStyle(PX.text2)
                    Picker("", selection: $quality) {
                        ForEach(qualities, id: \.self) { Text($0 == "HI_RES_LOSSLESS" ? "Hi-Res" : "CD Lossless").tag($0) }
                    }.labelsHidden().frame(width: 160).tint(PX.plex)
                }
            }.card()

            // Sources
            VStack(alignment: .leading, spacing: 14) {
                CardLabel(text: "Download sources")
                Toggle(isOn: $hiresOnly) { Text("Soulseek hi-res only").font(.system(size: 14)).foregroundStyle(PX.text) }
                    .toggleStyle(.switch).tint(PX.plex)
                Toggle(isOn: $pauseStreaming) { Text("Pause while Plex is streaming").font(.system(size: 14)).foregroundStyle(PX.text) }
                    .toggleStyle(.switch).tint(PX.plex)
                if let prio = store.settings?.source_priority, !prio.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Priority").font(.system(size: 11, weight: .semibold)).tracking(0.5).foregroundStyle(PX.muted)
                        ForEach(Array(prio.enumerated()), id: \.offset) { i, name in
                            HStack(spacing: 12) {
                                Text("\(i + 1)").font(.system(size: 11)).foregroundStyle(i == 0 ? PX.sp : PX.muted)
                                    .frame(width: 20, alignment: .leading)
                                Text(sourceDisplay(name)).font(.system(size: 14, weight: .semibold)).foregroundStyle(PX.text)
                                if i == 0 { Badge(text: "Primary", tint: PX.sp) }
                                Spacer()
                            }
                            .inset(padding: 12, radius: PX.controlRadius, fill: i == 0 ? PX.sp.opacity(0.07) : PX.bg3,
                                   stroke: i == 0 ? PX.sp.opacity(0.5) : PX.line)
                        }
                    }
                }
            }.card()

            // Plex stars → un-star = replace the wrong file
            VStack(alignment: .leading, spacing: 14) {
                CardLabel(text: "Plex stars")
                Toggle(isOn: $autostarEnabled) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Manage Plex stars").font(.system(size: 14)).foregroundStyle(PX.text)
                        Text("Auto-★ every placed track. Un-star a song in Plex/Plexamp to flag it wrong — Plexify attics it, blacklists that source, and re-acquires the correct copy.")
                            .font(.system(size: 12)).foregroundStyle(PX.muted)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }.toggleStyle(.switch).tint(PX.plex)
                if autostarEnabled {
                    Toggle(isOn: $autostarDryRun) {
                        Text("Dry run — log intended replacements without acting")
                            .font(.system(size: 13)).foregroundStyle(PX.text2)
                    }.toggleStyle(.switch).tint(PX.plex)
                }
            }.card()

            // Services (read-only health)
            VStack(alignment: .leading, spacing: 14) {
                CardLabel(text: "Connected services")
                ServiceLine(name: "Spotify", url: store.settings?.spotify_authed == true ? "authorized" : "not connected",
                            ok: store.settings?.spotify_authed)
                ServiceLine(name: "Plex", url: store.settings?.plex_url, ok: store.settings?.plex_token_set)
                ServiceLine(name: "slskd", url: store.settings?.slskd_url, ok: nil)
                ServiceLine(name: "Lidarr", url: store.settings?.lidarr_url, ok: nil)
                ServiceLine(name: "NAS downloader", url: store.settings?.nas_downloader_url, ok: nil)
                ServiceLine(name: "Telegram", url: store.settings?.telegram_configured == true ? "configured" : "off",
                            ok: store.settings?.telegram_enabled)
            }.card()

            HStack {
                Button { save() } label: { Text("Save changes") }.buttonStyle(PrimaryButtonStyle())
                if let a = store.lastAction { Text(a).font(.system(size: 12)).foregroundStyle(PX.ok) }
                Spacer()
            }
        }
        .task { await load() }
        .onChange(of: store.settings?.autofill_enabled) { _, _ in seed() }
    }

    private func load() async { await store.loadSettings(); seed() }
    private func seed() {
        guard let s = store.settings, !loaded else { return }
        enabled = s.autofill_enabled ?? false
        interval = s.autofill_interval_minutes ?? 30
        quality = s.quality_target ?? "LOSSLESS"
        hiresOnly = s.hires_only ?? false
        pauseStreaming = s.pause_when_streaming ?? true
        autostarEnabled = s.autostar_manage_enabled ?? false
        autostarDryRun = s.autostar_dry_run ?? false
        loaded = true
    }
    private func save() {
        Task {
            await store.saveSettings([
                "autofill_enabled": enabled,
                "autofill_interval_minutes": interval,
                "quality_target": quality,
                "hires_only": hiresOnly,
                "pause_when_streaming": pauseStreaming,
                "autostar_manage_enabled": autostarEnabled,
                "autostar_dry_run": autostarDryRun,
            ])
        }
    }
}

private struct ServiceLine: View {
    let name: String
    let url: String?
    let ok: Bool?
    var body: some View {
        HStack(spacing: 12) {
            Dot(color: ok == true ? PX.ok : (ok == false ? PX.danger : PX.muted), size: 8)
            Text(name).font(.system(size: 14, weight: .medium)).foregroundStyle(PX.text).frame(width: 140, alignment: .leading)
            Text(url ?? "—").font(.system(size: 13)).foregroundStyle(PX.muted).lineLimit(1)
            Spacer()
        }
        .padding(.vertical, 6)
        .overlay(alignment: .bottom) { Rectangle().fill(PX.line).frame(height: 1) }
    }
}
