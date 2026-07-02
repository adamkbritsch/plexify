import SwiftUI
import AppKit

// MARK: - Status mark (square, no glow — the theme's signature)

struct Dot: View {
    var color: Color
    var size: CGFloat = 8
    var body: some View {
        Rectangle().fill(color).frame(width: size, height: size)   // square, not a circle
    }
}

// Map a health status string → its status color (green/yellow/red/grey).
func healthColor(_ s: String?) -> Color {
    switch (s ?? "").lowercased() {
    case "green", "ok", "up":           return PX.ok
    case "yellow", "warn", "degraded":  return PX.warn
    case "red", "down", "error":        return PX.danger
    default:                            return PX.muted
    }
}

// MARK: - Badge / Pill (outlined, transparent, uppercase — "tags, not pills")

struct Badge: View {
    let text: String
    var tint: Color = PX.text2
    var border: Color? = nil        // defaults to lineStrong per the theme
    var body: some View {
        Text(text.uppercased())
            .font(.system(size: 11, weight: .semibold)).tracking(0.9)
            .foregroundStyle(tint)
            .padding(.horizontal, 8).padding(.vertical, 2)
            .background(RoundedRectangle(cornerRadius: PX.chipRadius).fill(Color.clear))
            .overlay(RoundedRectangle(cornerRadius: PX.chipRadius)
                .strokeBorder(border ?? PX.lineStrong, lineWidth: 1))
    }
}

// Autofill/transfer status → text tint (border stays neutral lineStrong per theme).
func statusBadgeTint(_ status: String?) -> Color {
    switch (status ?? "").lowercased() {
    case "queued":                              return PX.ok
    case "downloading", "inprogress":           return Color(hex: 0xB8E8FF)
    case "imported", "completed", "succeeded":  return PX.ok
    case "failed", "lookup_empty", "lookup_low_confidence",
         "errored", "cancelled", "timedout":    return PX.danger
    case "abandoned", "library_existing":       return PX.muted
    default:                                    return PX.text2
    }
}

struct Pill: View {
    let text: String
    var tint: Color = PX.muted
    var body: some View {
        Text(text.uppercased())
            .font(.system(size: 11, weight: .semibold)).tracking(1)
            .foregroundStyle(tint)
            .padding(.horizontal, 9).padding(.vertical, 3)
            .overlay(RoundedRectangle(cornerRadius: PX.chipRadius)
                .strokeBorder(tint == PX.muted ? PX.lineStrong : tint, lineWidth: 1))
    }
}

// MARK: - Buttons (square-ish, bordered, UPPERCASE micro-labels)

// .btn.primary — gold fill, black text.
struct PrimaryButtonStyle: ButtonStyle {
    @State private var hovering = false
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 12, weight: .semibold)).tracking(0.7).textCase(.uppercase)
            .foregroundStyle(Color.black)
            .padding(.horizontal, 14).padding(.vertical, 8)
            .background(RoundedRectangle(cornerRadius: PX.controlRadius)
                .fill(hovering ? Color(hex: 0xFFC81E) : PX.plex))
            .opacity(configuration.isPressed ? 0.9 : 1)
            .onHover { hovering = $0 }
    }
}

// .btn / .btn.danger — transparent, bordered, gold-on-hover.
struct GhostButtonStyle: ButtonStyle {
    var danger = false
    var small = false
    @State private var hovering = false
    func makeBody(configuration: Configuration) -> some View {
        let base = danger ? PX.danger : PX.text
        let edge = danger ? PX.danger : (hovering ? PX.plex : PX.lineStrong)
        configuration.label
            .font(.system(size: small ? 11 : 12, weight: .semibold))
            .tracking(0.7).textCase(.uppercase)
            .foregroundStyle(hovering && !danger ? PX.plex : base)
            .padding(.horizontal, small ? 10 : 14).padding(.vertical, small ? 5 : 8)
            .overlay(RoundedRectangle(cornerRadius: PX.controlRadius).strokeBorder(edge, lineWidth: 1))
            .opacity(configuration.isPressed ? 0.85 : 1)
            .onHover { hovering = $0 }
    }
}

// .dashctl-btn — faint fill, hairline border, gold hover; `active` = gold outline.
struct DashCtlButtonStyle: ButtonStyle {
    var active = false
    @State private var hovering = false
    func makeBody(configuration: Configuration) -> some View {
        let edge = active ? PX.plex : (hovering ? PX.plex : PX.lineStrong)
        configuration.label
            .font(.system(size: 12, weight: .semibold)).tracking(0.5).textCase(.uppercase)
            .foregroundStyle(active || hovering ? PX.plex : PX.text)
            .padding(.horizontal, 12).padding(.vertical, 7)
            .background(RoundedRectangle(cornerRadius: PX.controlRadius).fill(Color.white.opacity(0.04)))
            .overlay(RoundedRectangle(cornerRadius: PX.controlRadius).strokeBorder(edge, lineWidth: 1))
            .opacity(configuration.isPressed ? 0.85 : 1)
            .onHover { hovering = $0 }
    }
}

// MARK: - Search field (.search-input — bg is true black, gold focus outline)

struct SearchField: View {
    let placeholder: String
    @Binding var text: String
    @FocusState private var focused: Bool
    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "magnifyingglass").font(.system(size: 12)).foregroundStyle(PX.muted)
            TextField(placeholder, text: $text)
                .textFieldStyle(.plain).font(.system(size: 13)).foregroundStyle(PX.text)
                .focused($focused)
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
        .background(RoundedRectangle(cornerRadius: PX.controlRadius).fill(PX.bg))
        .overlay(RoundedRectangle(cornerRadius: PX.controlRadius)
            .strokeBorder(focused ? PX.plex : PX.lineStrong, lineWidth: 1))
    }
}

// MARK: - Stats (.stats: big number + uppercase label)

struct StatItem: View {
    let num: String
    let label: String
    var tint: Color = PX.text
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(num).font(.system(size: 28, weight: .bold)).foregroundStyle(tint).monospacedDigit()
            Text(label.uppercased()).font(.system(size: 11)).tracking(1).foregroundStyle(PX.muted)
        }
        .frame(minWidth: 90, alignment: .leading)
    }
}

struct StatStrip: View {
    let items: [(String, String)]   // (num, label)
    var body: some View {
        HStack(alignment: .top, spacing: 24) {
            ForEach(Array(items.enumerated()), id: \.offset) { _, it in StatItem(num: it.0, label: it.1) }
        }
    }
}

// MARK: - Source-health pill (.srch-pill — square dot, status-colored border+name)

struct SourceHealthPill: View {
    let name: String
    let detail: String
    let status: String?    // green | yellow | red | unknown/nil
    var body: some View {
        let sc = statusColorOrNil(status)
        let edge = sc ?? PX.lineStrong
        let nameColor = sc ?? PX.text
        let detailColor = (sc == PX.danger) ? PX.danger : PX.muted
        HStack(spacing: 9) {
            Dot(color: sc ?? PX.muted, size: 8)
            Text(name.uppercased()).font(.system(size: 11, weight: .semibold)).tracking(0.8)
                .foregroundStyle(nameColor).fixedSize()
            Text(detail.uppercased()).font(.system(size: 11)).tracking(0.4)
                .foregroundStyle(detailColor).lineLimit(1).truncationMode(.tail)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 13).padding(.vertical, 9)
        .frame(maxWidth: .infinity, alignment: .leading)
        .overlay(RoundedRectangle(cornerRadius: PX.chipRadius).strokeBorder(edge, lineWidth: 1))
        .help(detail)
    }
    func statusColorOrNil(_ s: String?) -> Color? {
        switch (s ?? "").lowercased() {
        case "green": return PX.ok
        case "yellow": return PX.warn
        case "red": return PX.danger
        default: return nil
        }
    }
}

// MARK: - Progress bar (.progress-bar — square-ish, solid/gradient fill, no shadow)

struct ProgressBarPX: View {
    var fraction: Double
    var height: CGFloat = 10
    var solid: Color? = nil
    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Rectangle().fill(Color.white.opacity(0.06))
                Rectangle()
                    .fill(solid.map { AnyShapeStyle($0) }
                          ?? AnyShapeStyle(LinearGradient(colors: [PX.sp, PX.plex],
                                                          startPoint: .leading, endPoint: .trailing)))
                    .frame(width: max(0, min(1, fraction)) * geo.size.width)
                    .animation(.easeInOut(duration: 0.4), value: fraction)
            }
        }
        .frame(height: height)
        .clipShape(RoundedRectangle(cornerRadius: height >= 8 ? 2 : 0))
    }
}

// MARK: - Quality chip (.feed-q / .live-qchip — quality IS meaningful, so it keeps color)

struct QualityChip: View {
    let label: String
    let hires: Bool
    var body: some View {
        let tint = hires ? PX.plex : PX.text2
        Text(label)
            .font(.system(size: 10.5, weight: .bold)).tracking(0.2)
            .foregroundStyle(tint)
            .padding(.horizontal, 7).padding(.vertical, 1)
            .background(RoundedRectangle(cornerRadius: PX.chipRadius)
                .fill(hires ? PX.plex.opacity(0.16) : Color.white.opacity(0.06)))
            .overlay(RoundedRectangle(cornerRadius: PX.chipRadius)
                .strokeBorder(hires ? PX.plex.opacity(0.40) : Color.white.opacity(0.12), lineWidth: 1))
    }
}

// MARK: - Dropdown (popover-based for exact chrome control; a single trailing caret)

struct DropMenu: View {
    var leading: String? = nil        // small uppercase label before the value (e.g. "FILL")
    let current: String
    let options: [String]
    var compact: Bool = false          // compact = 11px uppercase (control bar); else 15px (library)
    let onSelect: (String) -> Void
    @State private var open = false
    @State private var hovering = false
    var body: some View {
        Button { open.toggle() } label: {
            HStack(spacing: compact ? 7 : 6) {
                if let leading {
                    Text(leading.uppercased()).font(.system(size: 11, weight: .semibold)).tracking(0.5)
                        .foregroundStyle(PX.muted)
                }
                Text(compact ? current.uppercased() : current)
                    .font(.system(size: compact ? 11 : 15, weight: .semibold))
                    .tracking(compact ? 0.5 : 0)
                    .foregroundStyle(hovering && !compact ? PX.plex : PX.text)
                Image(systemName: "chevron.down").font(.system(size: compact ? 8 : 9)).foregroundStyle(PX.muted)
            }
            .padding(.horizontal, compact ? 11 : 9).padding(.vertical, compact ? 7 : 6)
            .background(compact ? RoundedRectangle(cornerRadius: PX.controlRadius).fill(Color.white.opacity(0.04)) : nil)
            .overlay(compact ? RoundedRectangle(cornerRadius: PX.controlRadius).strokeBorder(PX.lineStrong, lineWidth: 1) : nil)
            .background(!compact && hovering ? RoundedRectangle(cornerRadius: PX.controlRadius).fill(Color.white.opacity(0.07)) : nil)
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .popover(isPresented: $open, arrowEdge: .bottom) {
            VStack(alignment: .leading, spacing: 1) {
                ForEach(options, id: \.self) { opt in
                    Button { onSelect(opt); open = false } label: {
                        HStack(spacing: 12) {
                            Text(opt).font(.system(size: 14, weight: opt == current ? .semibold : .regular))
                                .foregroundStyle(opt == current ? PX.plex : PX.text)
                            Spacer(minLength: 16)
                            if opt == current {
                                Image(systemName: "checkmark").font(.system(size: 11, weight: .bold)).foregroundStyle(PX.plex)
                            }
                        }
                        .padding(.horizontal, 10).padding(.vertical, 8)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(HoverRowStyle())
                }
            }
            .padding(6).frame(minWidth: 190)
            .background(PX.bg3)
        }
    }
}

private struct HoverRowStyle: ButtonStyle {
    @State private var hovering = false
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .background(RoundedRectangle(cornerRadius: PX.controlRadius)
                .fill(hovering ? Color.white.opacity(0.07) : .clear))
            .onHover { hovering = $0 }
    }
}

// Source chip — NEUTRAL per the theme (color belongs to status, not source identity).
struct SourceTag: View {
    let source: String
    var body: some View {
        Text(source.uppercased())
            .font(.system(size: 10, weight: .semibold)).tracking(0.6)
            .foregroundStyle(PX.muted)
            .padding(.horizontal, 8).padding(.vertical, 2)
            .overlay(RoundedRectangle(cornerRadius: PX.chipRadius).strokeBorder(PX.lineStrong, lineWidth: 1))
    }
}
