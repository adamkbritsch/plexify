import SwiftUI
import AppKit

// Audiobooks — the pipeline view for the auto-m4b + organizer chain:
// drop folder → auto-m4b merge → untagged → (organizer: Audible match + tag) → Plex library,
// with a review queue for books the matcher wouldn't guess at.
struct AudiobooksView: View {
    @EnvironmentObject var store: PlexifyStore
    @State private var pendingDelete: AudiobookShelfItemDTO?
    @State private var confirmDelete = false
    @State private var deleteBusy = false
    @State private var deleteError: String?
    @State private var shelfSort = "Recently added"
    private static let shelfSorts = ["Recently added", "Title", "Author", "Most parts"]
    @State private var bookSearch = ""

    private var sortedShelf: [AudiobookShelfItemDTO] {
        let shelf = store.audiobookShelf ?? []
        switch shelfSort {
        case "Title":
            return shelf.sorted {
                ($0.title ?? "").localizedCaseInsensitiveCompare($1.title ?? "") == .orderedAscending
            }
        case "Author":
            return shelf.sorted {
                let a = ($0.author ?? "").localizedCaseInsensitiveCompare($1.author ?? "")
                if a != .orderedSame { return a == .orderedAscending }
                return ($0.title ?? "").localizedCaseInsensitiveCompare($1.title ?? "") == .orderedAscending
            }
        case "Most parts":
            return shelf.sorted {
                if ($0.tracks ?? 0) != ($1.tracks ?? 0) { return ($0.tracks ?? 0) > ($1.tracks ?? 0) }
                return ($0.title ?? "").localizedCaseInsensitiveCompare($1.title ?? "") == .orderedAscending
            }
        default:   // Recently added
            return shelf.sorted { ($0.added_at ?? 0) > ($1.added_at ?? 0) }
        }
    }

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
                    if let w = st?.working_on, let f = w.file {
                        Text("organizing: \(f)\(w.stage.map { " — \($0)" } ?? "")")
                            .font(.system(size: 11)).foregroundStyle(PX.sp).lineLimit(1)
                    } else if let msg = store.audiobookOrganizeMsg {
                        Text(msg).font(.system(size: 11)).foregroundStyle(PX.muted).lineLimit(1)
                    }
                    Spacer()
                    Button { Task { await store.organizeAudiobooksNow() } } label: {
                        Label("Organize now", systemImage: "wand.and.rays")
                    }.buttonStyle(DashCtlButtonStyle())
                        .disabled(st?.feature_enabled != true || st?.reachable != true
                                  || store.audiobookOrganizeMsg == "starting…")
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

            // Suggested — books like the library's (+ a search box); everything shown is
            // confirmed available on Soulseek right now, one click to want it
            do {
                let searching = store.audiobookSearchResults != nil
                let rows = searching ? (store.audiobookSearchResults ?? [])
                                     : (store.audiobookSuggestions ?? [])
                VStack(alignment: .leading, spacing: 12) {
                    HStack(spacing: 8) {
                        Text(searching ? "Search results" : "Suggested")
                            .font(.system(size: 15, weight: .semibold)).foregroundStyle(PX.text)
                        if store.audiobookSearching || store.audiobookSuggestGenerating {
                            ProgressView().scaleEffect(0.5)
                        } else {
                            Badge(text: "\(rows.count)", tint: PX.muted)
                        }
                        Spacer()
                        Text("everything here is on Soulseek now")
                            .font(.system(size: 11)).foregroundStyle(PX.muted)
                    }
                    HStack(spacing: 8) {
                        Image(systemName: "magnifyingglass").foregroundStyle(PX.muted)
                            .font(.system(size: 12))
                        TextField("Search for any audiobook…", text: $bookSearch)
                            .textFieldStyle(.plain).font(.system(size: 13)).foregroundStyle(PX.text)
                            .onSubmit { Task { await store.searchAudiobooks(bookSearch) } }
                        if !bookSearch.isEmpty {
                            Button {
                                bookSearch = ""; store.clearAudiobookSearch()
                            } label: { Image(systemName: "xmark.circle.fill") }
                                .buttonStyle(.plain).foregroundStyle(PX.muted)
                        }
                    }
                    .padding(8).background(PX.bg3)
                    .overlay(RoundedRectangle(cornerRadius: PX.controlRadius)
                        .strokeBorder(PX.line, lineWidth: 1))
                    if searching && rows.isEmpty && !store.audiobookSearching {
                        Text("Nothing on Soulseek for that — try different words.")
                            .font(.system(size: 12)).foregroundStyle(PX.muted)
                    }
                    ForEach(rows.prefix(12)) { s in
                        HStack(spacing: 10) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(s.title ?? "?").font(.system(size: 13, weight: .medium))
                                    .foregroundStyle(PX.text).lineLimit(1)
                                HStack(spacing: 6) {
                                    Text(s.author ?? "").font(.system(size: 12))
                                        .foregroundStyle(PX.text2).lineLimit(1)
                                    if let r = s.runtime_min, r > 0 {
                                        Text(verbatim: "\(r / 60)h \(r % 60)m")
                                            .font(.system(size: 11)).foregroundStyle(PX.muted)
                                    }
                                }
                            }
                            if let why = s.reason, !why.isEmpty {
                                Text(why).font(.system(size: 11)).foregroundStyle(PX.muted)
                                    .lineLimit(1)
                                    .padding(.horizontal, 7).padding(.vertical, 2)
                                    .background(Capsule().fill(Color.white.opacity(0.05)))
                            }
                            Spacer()
                            Button {
                                Task { _ = await store.wantAudiobook(s) }
                            } label: {
                                Label("Download", systemImage: "arrow.down.circle")
                            }.buttonStyle(GhostButtonStyle(small: true))
                            Button {
                                Task { await store.dismissAudiobookSuggestion(asin: s.asin ?? "") }
                            } label: {
                                Image(systemName: "xmark")
                            }.buttonStyle(GhostButtonStyle(small: true))
                                .help("Not interested — hides this suggestion")
                        }
                        .padding(.vertical, 6).padding(.horizontal, 10)
                        .inset(padding: 0, radius: PX.controlRadius, fill: PX.bg3, stroke: PX.line)
                    }
                }.card()
            }

            // Wanted — the download list: tries Soulseek now, retries for days
            if let wants = store.audiobookWanted, !wants.isEmpty {
                VStack(alignment: .leading, spacing: 12) {
                    HStack(spacing: 8) {
                        Text("Wanted").font(.system(size: 15, weight: .semibold))
                            .foregroundStyle(PX.text)
                        Badge(text: "\(wants.count)", tint: PX.muted)
                        Spacer()
                    }
                    ForEach(wants) { w in
                        HStack(spacing: 10) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(w.title ?? "?").font(.system(size: 13, weight: .medium))
                                    .foregroundStyle(PX.text).lineLimit(1)
                                Text(w.author ?? "").font(.system(size: 12))
                                    .foregroundStyle(PX.text2).lineLimit(1)
                            }
                            Spacer()
                            Text(wantLabel(w)).font(.system(size: 11))
                                .foregroundStyle(wantTint(w)).lineLimit(1)
                            if w.status != "delivered" {
                                Button {
                                    Task { _ = await store.unwantAudiobook(asin: w.asin ?? "",
                                                                           title: w.title ?? "") }
                                } label: {
                                    Image(systemName: "xmark")
                                }.buttonStyle(GhostButtonStyle(small: true))
                                    .help("Stop trying to get this book")
                            }
                        }
                        .padding(.vertical, 6).padding(.horizontal, 10)
                        .inset(padding: 0, radius: PX.controlRadius, fill: PX.bg3, stroke: PX.line)
                    }
                }.card()
            }

            // Library — every book Plex knows, as a cover-art shelf; deleting a book is a
            // soft-delete (folder → trash/, never unlinked; Plexify NEVER destroys audio)
            VStack(alignment: .leading, spacing: 14) {
                HStack(spacing: 12) {
                    Text("Library").font(.system(size: 15, weight: .semibold)).foregroundStyle(PX.text)
                    Badge(text: "\((store.audiobookShelf ?? []).count)", tint: PX.muted)
                    DropMenu(leading: "sort", current: shelfSort,
                             options: Self.shelfSorts, compact: true) { shelfSort = $0 }
                    Spacer()
                    if let e = deleteError {
                        Text(e).font(.system(size: 11)).foregroundStyle(PX.danger).lineLimit(1)
                    } else {
                        Text("deleting moves the book to the trash folder on the NAS")
                            .font(.system(size: 11)).foregroundStyle(PX.muted)
                    }
                }
                let shelf = sortedShelf
                if shelf.isEmpty {
                    Text("No books indexed yet.")
                        .font(.system(size: 13)).foregroundStyle(PX.muted)
                        .frame(maxWidth: .infinity).padding(.vertical, 20)
                } else {
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 142, maximum: 188),
                                                 spacing: 14, alignment: .top)],
                              alignment: .leading, spacing: 18) {
                        ForEach(shelf) { b in
                            AudiobookShelfCard(book: b,
                                               coverURL: URL(string: store.base + "/api/audiobooks/cover/\(b.key ?? 0)"),
                                               disabled: (b.rel_dir ?? "").isEmpty || deleteBusy) {
                                pendingDelete = b
                                confirmDelete = true
                            }
                        }
                    }
                }
            }.card()
            .confirmationDialog(
                "Delete \"\(pendingDelete?.title ?? "")\"?",
                isPresented: $confirmDelete,
                titleVisibility: .visible,
                presenting: pendingDelete
            ) { b in
                // presenting: passes the VALUE into the closures — immune to the
                // dismissal-order state-nil race of the isPresented-only pattern
                Button("Move to trash", role: .destructive) {
                    deleteBusy = true
                    deleteError = nil
                    Task {
                        let (ok, err) = await store.deleteAudiobook(relDir: b.rel_dir ?? "")
                        if !ok { deleteError = "delete failed: \(err ?? "unknown")" }
                        deleteBusy = false
                    }
                }
                Button("Cancel", role: .cancel) {}
            } message: { _ in
                Text("The book folder moves to the NAS trash folder (recoverable) and the Plex entry is removed. Nothing is permanently deleted.")
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
            await store.loadAudiobookShelf()
            await store.loadAudiobookSuggestions()
            await store.loadAudiobookWanted()
            var n = 0
            while !Task.isCancelled {
                await store.loadAudiobooks()
                n += 1
                if n % 6 == 0 { await store.loadAudiobookShelf() }   // shelf every ~30s
                if n % 3 == 0 { await store.loadAudiobookWanted() }  // wanted every ~15s
                if n % 24 == 0 { await store.loadAudiobookSuggestions() }
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

    private func wantLabel(_ w: AudiobookWantedDTO) -> String {
        switch w.status {
        case "downloading":
            return "downloading\(w.total_mb.map { " · \($0) MB" } ?? "")"
        case "delivered":
            return "delivered — organizing"
        case "gave_up":
            return "gave up after \(w.attempts ?? 0) tries"
        default:
            let s = w.next_try_in_s ?? 0
            if s <= 0 { return "searching soon" }
            let h = s / 3600, m = (s % 3600) / 60
            return "retry in \(h > 0 ? "\(h)h " : "")\(m)m (try \((w.attempts ?? 0) + 1))"
        }
    }

    private func wantTint(_ w: AudiobookWantedDTO) -> Color {
        switch w.status {
        case "downloading": return PX.sp
        case "delivered": return PX.sp
        case "gave_up": return PX.danger
        default: return PX.muted
        }
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
    @State private var confirmDiscard = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text(item.file ?? "?").font(.system(size: 13, weight: .medium)).foregroundStyle(PX.text).lineLimit(1)
                if let r = item.reason { Badge(text: r.replacingOccurrences(of: "_", with: " "), tint: PX.warn) }
                Spacer()
            }
            if let g = item.guess, let t = g["title"], !t.isEmpty {
                HStack(spacing: 8) {
                    Text("guessed: \(t)\(g["author"].map { " — \($0)" } ?? "")")
                        .font(.system(size: 11)).foregroundStyle(PX.muted).lineLimit(1)
                    Button {
                        act { await resolveGuess(title: t, author: g["author"] ?? "") }
                    } label: {
                        Label("File the guess", systemImage: "checkmark.circle")
                    }
                    .buttonStyle(GhostButtonStyle(small: true))
                    .disabled(busy || (g["author"] ?? "").isEmpty)
                    .help((g["author"] ?? "").isEmpty
                          ? "The guess has no author — type one below"
                          : "File this book as \"\(t)\" by \(g["author"] ?? "")")
                }
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
                Spacer()
                Button { confirmDiscard = true } label: {
                    Label("Discard", systemImage: "trash")
                }
                .buttonStyle(GhostButtonStyle(small: true)).disabled(busy)
                .help("Don't want this drop? Moves the file to the NAS trash folder.")
                if busy { ProgressView().scaleEffect(0.5) }
                if let err { Text(err).font(.system(size: 11)).foregroundStyle(PX.danger).lineLimit(1) }
            }
            .confirmationDialog("Discard \"\(item.file ?? "")\"?",
                                isPresented: $confirmDiscard, titleVisibility: .visible) {
                Button("Move to trash", role: .destructive) {
                    act {
                        let (ok, e) = await store.discardAudiobookReview(file: item.file ?? "")
                        if !ok { err = "discard failed: \(e ?? "unknown")" }
                    }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("The file moves to the NAS trash folder — recoverable, never permanently deleted.")
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

    private func resolveGuess(title: String, author: String) async {
        let ok = await store.resolveAudiobook(file: item.file ?? "",
                                              author: author, title: title)
        if !ok { err = "resolve failed" }
    }
}


// One book on the shelf: square cover, title + author, trash on hover.
private struct AudiobookShelfCard: View {
    let book: AudiobookShelfItemDTO
    let coverURL: URL?
    let disabled: Bool
    let onDelete: () -> Void
    @State private var hovering = false

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            ZStack(alignment: .topTrailing) {
                AsyncImage(url: coverURL) { phase in
                    if let img = phase.image {
                        img.resizable().aspectRatio(contentMode: .fill)
                    } else {
                        ZStack {
                            Rectangle().fill(PX.bg3)
                            Image(systemName: "book.closed")
                                .font(.system(size: 30)).foregroundStyle(PX.muted)
                        }
                    }
                }
                .frame(minWidth: 0, maxWidth: .infinity)
                .aspectRatio(1, contentMode: .fit)
                .clipShape(RoundedRectangle(cornerRadius: PX.controlRadius))
                .overlay(RoundedRectangle(cornerRadius: PX.controlRadius)
                    .strokeBorder(hovering ? PX.lineStrong : PX.line, lineWidth: 1))

                Button(action: onDelete) {
                    Image(systemName: "trash")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(PX.text)
                        .padding(6)
                        .background(Circle().fill(Color.black.opacity(0.72)))
                        .overlay(Circle().strokeBorder(PX.line, lineWidth: 1))
                }
                .buttonStyle(.plain)
                .disabled(disabled)
                .help(disabled && (book.rel_dir ?? "").isEmpty
                      ? "No file path known for this entry"
                      : "Move this book to the trash folder")
                .padding(6)
                .opacity(hovering ? 1 : 0)

                if let t = book.tracks, t > 1 {
                    VStack {
                        Spacer()
                        HStack {
                            Text("\(t) parts")
                                .font(.system(size: 10, weight: .semibold))
                                .foregroundStyle(PX.text)
                                .padding(.horizontal, 6).padding(.vertical, 3)
                                .background(Capsule().fill(Color.black.opacity(0.72)))
                                .overlay(Capsule().strokeBorder(PX.line, lineWidth: 1))
                            Spacer()
                        }
                    }.padding(6)
                }
            }
            Text(book.title ?? "?")
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(PX.text).lineLimit(2)
                .multilineTextAlignment(.leading)
                .fixedSize(horizontal: false, vertical: true)
            Text(book.author ?? "")
                .font(.system(size: 11)).foregroundStyle(PX.text2).lineLimit(1)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(Rectangle())
        .onHover { hovering = $0 }
        .animation(.easeInOut(duration: 0.12), value: hovering)
    }
}
