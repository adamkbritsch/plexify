import SwiftUI

struct JobsView: View {
    @EnvironmentObject var store: PlexifyStore
    @State private var detailJob: Int?
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            VStack(alignment: .leading, spacing: 8) {
                PageTitle(text: "Jobs", subtitle: "Background operations — syncs, imports, backfills.")
            }.card()

            if !store.activeJobs.isEmpty {
                VStack(alignment: .leading, spacing: 12) {
                    CardLabel(text: "Active")
                    ForEach(store.activeJobs) { j in
                        ActiveJobRow(job: j).onTapGesture { if let id = j.id { detailJob = id } }
                    }
                }.card()
            }

            VStack(alignment: .leading, spacing: 12) {
                CardLabel(text: "Recent")
                if store.recentJobs.isEmpty {
                    Text("No jobs yet.").font(.system(size: 13)).foregroundStyle(PX.muted)
                        .frame(maxWidth: .infinity).padding(.vertical, 16)
                } else {
                    ForEach(store.recentJobs) { j in
                        RecentJobRow(job: j).onTapGesture { if let id = j.id { detailJob = id } }
                    }
                }
            }.card()
        }
        .task { await store.loadJobs() }
        .sheet(item: Binding(get: { detailJob.map { IdBox(id: $0) } }, set: { detailJob = $0?.id })) { box in
            JobDetailSheet(jobId: box.id) { detailJob = nil }
                .environmentObject(store)
        }
    }
}

private struct IdBox: Identifiable { let id: Int }

private struct ActiveJobRow: View {
    let job: JobDTO
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(job.title ?? job.kind ?? "Job #\(job.id ?? 0)").font(.system(size: 14, weight: .medium))
                    .foregroundStyle(PX.text)
                Spacer()
                Badge(text: job.status ?? "running", tint: statusBadgeTint(job.status))
            }
            if (job.progress_total ?? 0) > 0 {
                ProgressBarPX(fraction: job.fraction, height: 8)
                Text("\(job.progress_step ?? "") \(job.progress_current ?? 0)/\(job.progress_total ?? 0)")
                    .font(.system(size: 11)).foregroundStyle(PX.muted)
            } else if let step = job.progress_step {
                Text(step).font(.system(size: 11)).foregroundStyle(PX.muted)
            }
        }
        .inset(padding: 12, radius: PX.controlRadius, fill: .clear, stroke: PX.line)
        .contentShape(Rectangle())
    }
}

private struct RecentJobRow: View {
    let job: JobDTO
    var body: some View {
        HStack(spacing: 12) {
            Text(verbatim: "#\(job.id ?? 0)").font(.system(size: 12)).monospacedDigit().foregroundStyle(PX.muted)
                .frame(width: 56, alignment: .leading)
            VStack(alignment: .leading, spacing: 1) {
                Text(job.title ?? job.kind ?? "—").font(.system(size: 14)).foregroundStyle(PX.text).lineLimit(1)
                if let k = job.kind { Text(k).font(.system(size: 11)).foregroundStyle(PX.muted) }
            }
            Spacer()
            Text(ago(job.finished_at ?? job.started_at)).font(.system(size: 11)).foregroundStyle(PX.muted)
            Badge(text: job.status ?? "—", tint: statusBadgeTint(job.status))
        }
        .padding(.vertical, 10).padding(.horizontal, 4)
        .overlay(alignment: .bottom) { Rectangle().fill(PX.line).frame(height: 1) }
        .contentShape(Rectangle())
    }
}

private struct JobDetailSheet: View {
    @EnvironmentObject var store: PlexifyStore
    let jobId: Int
    let onClose: () -> Void
    var body: some View {
        let j = store.jobDetail
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(j?.title ?? "Job #\(jobId)").font(.system(size: 16, weight: .semibold)).foregroundStyle(PX.text)
                Spacer()
                if let s = j?.status { Badge(text: s, tint: statusBadgeTint(s)) }
                Button { onClose() } label: { Image(systemName: "xmark").font(.system(size: 12)) }
                    .buttonStyle(.plain).foregroundStyle(PX.muted).padding(.leading, 8)
            }
            .padding(16)
            .overlay(alignment: .bottom) { Rectangle().fill(PX.line).frame(height: 1) }

            if (j?.progress_total ?? 0) > 0 {
                VStack(alignment: .leading, spacing: 4) {
                    ProgressBarPX(fraction: j?.fraction ?? 0, height: 8)
                    Text("\(j?.progress_step ?? "") \(j?.progress_current ?? 0)/\(j?.progress_total ?? 0)")
                        .font(.system(size: 11)).foregroundStyle(PX.muted)
                }.padding(16)
            }

            ScrollView {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(j?.events ?? []) { e in
                        HStack(alignment: .top, spacing: 8) {
                            Text(e.level ?? "info").font(.system(size: 10, weight: .semibold))
                                .foregroundStyle(e.level == "error" ? PX.danger : PX.muted)
                                .frame(width: 42, alignment: .leading)
                            Text(e.message ?? "").font(.system(size: 12, design: .monospaced))
                                .foregroundStyle(PX.text2).frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                }.padding(16).frame(maxWidth: .infinity, alignment: .leading)
            }
            .background(Color(hex: 0x0A0C10))
        }
        .frame(width: 640, height: 520)
        .background(PX.bg2)
        .task { await store.loadJobDetail(jobId) }
    }
}
