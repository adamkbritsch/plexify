import SwiftUI
import AppKit

// Audiobooks — the pipeline view for the auto-m4b + organizer chain:
// drop folder → auto-m4b merge → untagged → (organizer: Audible match + tag) → Plex library,
// with a review queue for books the matcher wouldn't guess at.
struct AudiobooksView: View {
    @EnvironmentObject var store: PlexifyStore

    private var st: AudiobooksStatusDTO? { store.audiobooks }
    private var reviewItems: [AudiobookDTO] {
        // The daemon reports the queue from the review FOLDER itself (joined with each file's
        // ledger record) — deriving it from the recent-records window silently dropped
        // outstanding items whenever a busy day pushed them past the window.
        if let items = st?.review_items { return items }
        // fallback for an older daemon: infer from the recent window (ledger is append-only —
        // a review entry is actionable only while no LATER organized record names the file)
        let recent = st?.recent ?? []
        var organized = Set<String>()
        var seen = Set<String>()
        var out: [AudiobookDTO] = []
        for r in recent {
            guard let f = r.file else { continue }
            if r.status == "organized" { organized.insert(f) }
            else if r.status == "review" && !organized.contains(f) && !seen.contains(f) {
                seen.insert(f); out.append(r)
            }
        }
        return out
    }
    private var organizedItems: [AudiobookDTO] {
        (st?.recent ?? []).filter { $0.status == "organized" }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            VStack(alignment: .leading, spacing: 8) {
                PageTitle(text: "Audiobooks",
                          subtitle: "Drop a book (folder of mp3s or a single file) into the drop folder — it gets merged to one chapterized m4b, matched against Audible, tagged, and filed into your Plex audiobook library.")
                if st?.feature_enabled != true {
                    Text("Audiobooks are OFF — enable them in Settings › Audiobooks.")
                        .font(.system(size: 12)).foregroundStyle(PX.warn)
                } else if st?.reachable != true {
                    Text("Organizer daemon unreachable — it runs on the NAS (plexify-downloader).")
                        .font(.system(size: 12)).foregroundStyle(PX.warn)
                } else if st?.dirs_ok != true {
                    Text("Audiobook folders not mounted in the daemon — redeploy it with the AUDIOBOOKS volumes (see docs/AUDIOBOOKS.md).")
                        .font(.system(size: 12)).foregroundStyle(PX.warn)
                }
            }.card()

            // Pipeline stages
            VStack(alignment: .leading, spacing: 14) {
                HStack(spacing: 8) {
                    Text("Pipeline").font(.system(size: 15, weight: .semibold)).foregroundStyle(PX.text)
                    Spacer()
                    Button { Task { await store.organizeAudiobooksNow() } } label: {
                        Label("Organize now", systemImage: "wand.and.rays")
                    }.buttonStyle(DashCtlButtonStyle())
                        .disabled(st?.feature_enabled != true || st?.reachable != true)
                    Button { openDropFolder() } label: {
                        Label("Open drop folder", systemImage: "folder")
                    }.buttonStyle(DashCtlButtonStyle())
                }
                HStack(spacing: 12) {
                    stage("Dropped", st?.dropped, help: "In the drop folder, waiting for auto-m4b")
                    stageArrow()
                    stage("Converting", st?.converting, help: "auto-m4b merging mp3s into a chapterized m4b")
                    stageArrow()
                    stage("Ready to tag", st?.untagged, help: "Merged m4b waiting for the organizer's next pass")
                    stageArrow()
                    stage("Needs review", st?.review, warn: (st?.review ?? 0) > 0,
                          help: "No confident Audible match — resolve below")
                    stageArrow()
                    stage("In library", st?.organized_total, ok: true,
                          help: "Tagged and filed into the Plex audiobook library")
                }
            }.card()

            // Converter live progress (auto-m4b): the active merge + what's waiting behind it
            if let conv = st?.converter, conv.active != nil || !(conv.queue ?? []).isEmpty {
                VStack(alignment: .leading, spacing: 12) {
                    HStack(spacing: 8) {
                        Text("Converting now").font(.system(size: 15, weight: .semibold))
                            .foregroundStyle(PX.text)
                        if let q = conv.queue, !q.isEmpty {
                            Badge(text: "\(q.count) queued", tint: PX.muted)
                        }
                        Spacer()
                    }
                    if let a = conv.active {
                        VStack(alignment: .leading, spacing: 6) {
                            HStack(spacing: 8) {
                                Text(a.book ?? "?").font(.system(size: 13, weight: .medium))
                                    .foregroundStyle(PX.text).lineLimit(1)
                                if a.stalled == true {
                                    Badge(text: "stalled?", tint: PX.warn)
                                }
                                Spacer()
                                Text(convDetail(a)).font(.system(size: 11)).foregroundStyle(PX.muted)
                            }
                            ProgressView(value: Double(a.percent ?? 0), total: 100)
                                .tint(PX.sp)
                        }
                        .inset(padding: 12, radius: PX.controlRadius, fill: PX.bg3, stroke: PX.line)
                    } else {
                        Text("Between books — auto-m4b picks up the next one on its minute cycle.")
                            .font(.system(size: 12)).foregroundStyle(PX.muted)
                    }
                    if let q = conv.queue, !q.isEmpty {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("UP NEXT").font(.system(size: 10, weight: .semibold)).tracking(0.6)
                                .foregroundStyle(PX.muted)
                            ForEach(Array(q.prefix(6).enumerated()), id: \.offset) { _, b in
                                HStack(spacing: 8) {
                                    Text(b.book ?? "?").font(.system(size: 12))
                                        .foregroundStyle(PX.text2).lineLimit(1)
                                    Spacer()
                                    Text(sizeLabel(b.src_bytes)).font(.system(size: 11))
                                        .foregroundStyle(PX.muted)
                                }
                            }
                            if q.count > 6 {
                                Text("+ \(q.count - 6) more").font(.system(size: 11))
                                    .foregroundStyle(PX.muted)
                            }
                        }
                    }
                }.card()
            }

            // Review queue
            if !reviewItems.isEmpty {
                VStack(alignment: .leading, spacing: 12) {
                    HStack(spacing: 8) {
                        Text("Needs review").font(.system(size: 15, weight: .semibold)).foregroundStyle(PX.text)
                        Badge(text: "\(reviewItems.count)", tint: PX.warn)
                        Spacer()
                    }
                    Text("The matcher refuses to guess: pick the right edition, or type the author and title yourself (books that aren't on Audible get filed with your values).")
                        .font(.system(size: 12)).foregroundStyle(PX.muted)
                    ForEach(reviewItems) { item in
                        ReviewRow(item: item)
                    }
                }.card()
            }

            // Recently organized
            VStack(alignment: .leading, spacing: 0) {
                HStack {
                    Text("RECENTLY ORGANIZED").font(.system(size: 11, weight: .semibold)).tracking(0.5).foregroundStyle(PX.muted)
                    Spacer()
                }
                .padding(.vertical, 8).padding(.horizontal, 12)
                .overlay(alignment: .bottom) { Rectangle().fill(PX.lineStrong).frame(height: 1) }
                if organizedItems.isEmpty {
                    Text("Nothing organized yet — drop a book to get started.")
                        .font(.system(size: 13)).foregroundStyle(PX.muted)
                        .frame(maxWidth: .infinity).padding(.vertical, 20)
                } else {
                    ForEach(organizedItems.prefix(20)) { b in
                        HStack(spacing: 12) {
                            AsyncImage(url: URL(string: b.cover_url ?? "")) { img in
                                img.resizable().aspectRatio(contentMode: .fill)
                            } placeholder: {
                                Rectangle().fill(PX.bg3)
                            }
                            .frame(width: 40, height: 40).clipped()
                            VStack(alignment: .leading, spacing: 2) {
                                Text(b.title ?? b.file ?? "?").font(.system(size: 13, weight: .medium))
                                    .foregroundStyle(PX.text).lineLimit(1)
                                Text(b.author ?? "").font(.system(size: 12)).foregroundStyle(PX.text2).lineLimit(1)
                            }
                            Spacer()
                            if let s = b.score { Text("match \(s)").font(.system(size: 11)).foregroundStyle(PX.muted) }
                            Text(ago(b.ts)).font(.system(size: 11)).foregroundStyle(PX.muted)
                                .frame(width: 70, alignment: .trailing)
                        }
                        .padding(.vertical, 8).padding(.horizontal, 12)
                        .overlay(alignment: .bottom) { Rectangle().fill(PX.line).frame(height: 1) }
                    }
                }
            }.card(padding: 0)
        }
        .task {
            // page-local poll while visible
            while !Task.isCancelled {
                await store.loadAudiobooks()
                try? await Task.sleep(nanoseconds: 5_000_000_000)
            }
        }
    }

    @ViewBuilder
    private func stage(_ name: String, _ n: Int?, ok: Bool = false, warn: Bool = false,
                       help: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(name.uppercased()).font(.system(size: 10, weight: .semibold)).tracking(0.6)
                .foregroundStyle(PX.muted)
            Text(verbatim: "\(n ?? 0)").font(.system(size: 22, weight: .semibold))
                .foregroundStyle(warn ? PX.warn : (ok ? PX.sp : PX.text))
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .inset(padding: 12, radius: PX.controlRadius, fill: PX.bg3, stroke: PX.line)
        .help(help)
    }

    @ViewBuilder
    private func stageArrow() -> some View {
        Text(verbatim: ">").font(.system(size: 13, weight: .semibold)).foregroundStyle(PX.muted)
    }

    private func convDetail(_ a: ConvertingBookDTO) -> String {
        var parts: [String] = []
        if a.phase == "assembling" {
            parts.append("assembling the m4b")
        } else if let d = a.done, let f = a.files {
            parts.append("\(d)/\(f) tracks")
        }
        if let p = a.percent { parts.append("\(p)%") }
        if let s = a.src_bytes, s > 0 { parts.append(sizeLabel(s)) }
        return parts.joined(separator: " · ")
    }

    private func sizeLabel(_ bytes: Int64?) -> String {
        guard let b = bytes, b > 0 else { return "" }
        let mb = Double(b) / 1_048_576
        return mb >= 1024 ? String(format: "%.1f GB", mb / 1024) : String(format: "%.0f MB", mb)
    }

    private func openDropFolder() {
        let path = store.settings?.audiobook_drop_path
            ?? "/Volumes/MediaVolume3/plexify-imports"
        NSWorkspace.shared.open(URL(fileURLWithPath: path, isDirectory: true))
    }
}

private struct ReviewRow: View {
    @EnvironmentObject var store: PlexifyStore
    let item: AudiobookDTO
    @State private var manualAuthor = ""
    @State private var manualTitle = ""
    @State private var busy = false
    @State private var err: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text(item.file ?? "?").font(.system(size: 13, weight: .medium)).foregroundStyle(PX.text).lineLimit(1)
                if let r = item.reason { Badge(text: r.replacingOccurrences(of: "_", with: " "), tint: PX.warn) }
                Spacer()
            }
            if let g = item.guess, let t = g["title"] {
                Text("guessed: \(t)\(g["author"].map { " — \($0)" } ?? "")")
                    .font(.system(size: 11)).foregroundStyle(PX.muted)
            }
            if let cands = item.candidates, !cands.isEmpty {
                ForEach(Array(cands.enumerated()), id: \.offset) { _, c in
                    Button {
                        act { await resolve(asin: c.asin) }
                    } label: {
                        HStack(spacing: 6) {
                            Text(verbatim: "Use:").foregroundStyle(PX.muted)
                            Text("\(c.title ?? "?") — \((c.authors ?? []).joined(separator: ", "))")
                                .lineLimit(1)
                        }.font(.system(size: 12))
                    }
                    .buttonStyle(GhostButtonStyle(small: true)).disabled(busy)
                }
            }
            HStack(spacing: 8) {
                TextField("Author", text: $manualAuthor)
                    .textFieldStyle(.plain).font(.system(size: 12)).foregroundStyle(PX.text)
                    .padding(6).background(PX.bg3)
                    .overlay(Rectangle().strokeBorder(PX.line, lineWidth: 1))
                    .frame(maxWidth: 180)
                TextField("Title", text: $manualTitle)
                    .textFieldStyle(.plain).font(.system(size: 12)).foregroundStyle(PX.text)
                    .padding(6).background(PX.bg3)
                    .overlay(Rectangle().strokeBorder(PX.line, lineWidth: 1))
                    .frame(maxWidth: 240)
                Button { act { await resolve(asin: nil) } } label: { Text("File it") }
                    .buttonStyle(GhostButtonStyle(small: true))
                    .disabled(busy || manualAuthor.isEmpty || manualTitle.isEmpty)
                if busy { ProgressView().scaleEffect(0.5) }
                if let err { Text(err).font(.system(size: 11)).foregroundStyle(PX.danger).lineLimit(1) }
            }
        }
        .inset(padding: 12, radius: PX.controlRadius, fill: PX.bg3, stroke: PX.line)
        .opacity(busy ? 0.6 : 1)
    }

    private func act(_ f: @escaping () async -> Void) { busy = true; err = nil; Task { await f(); busy = false } }

    private func resolve(asin: String?) async {
        let ok = await store.resolveAudiobook(
            file: item.file ?? "",
            asin: asin,
            author: asin == nil ? manualAuthor : nil,
            title: asin == nil ? manualTitle : nil)
        if !ok { err = "resolve failed" }
    }
}
