import SwiftUI
import AppKit

// Design tokens ported from the web UI's ACTIVE style layer — the "PLEXIFY OLED THEME"
// override at the bottom of style.css (it redefines :root and !important-overrides the
// older grey/gradient layer, so this is what actually renders). Dark theme only.
//
// Design language: true-black OLED, square structural geometry, hairline separation.
// BANNED (deliberately, to avoid "AI-generated" tells): gradients on chrome, glassmorphism/
// blur, border radii on cards, pill shapes, colored glow shadows, cyan/indigo/pink accents.
// Color belongs to STATUS only. Hierarchy comes from type scale + density, not elevation.
enum PX {
    static let bg          = Color(hex: 0x000000)   // the page — the ONLY true black
    static let bg2         = Color(hex: 0x0E0E0E)   // cards / sections
    static let bg3         = Color(hex: 0x181818)   // nested + hover + popups/menus
    static let line        = Color(hex: 0x242424)
    static let lineStrong  = Color(hex: 0x343434)
    static let text        = Color(hex: 0xF2F2F2)
    static let text2       = Color(hex: 0xA4ABB8)
    static let muted       = Color(hex: 0x8A8A8A)

    static let plex        = Color(hex: 0xEBAF00)   // brand GOLD — actions, focus, emphasis, links
    static let sp          = Color(hex: 0x1ED760)   // brand GREEN — success / connected ONLY
    static let td          = Color(hex: 0xEBAF00)   // cyan retired → gold
    static let accent      = Color(hex: 0xF5C518)   // condense / wizard accent gold
    static let danger      = Color(hex: 0xFF3B30)
    static let warn        = Color(hex: 0xFFB000)
    static let ok          = Color(hex: 0x1ED760)

    // Geometry: structure is SQUARE; controls get a subtle 6px; chips 4px. No shadows/glows.
    static let cardRadius: CGFloat = 0
    static let controlRadius: CGFloat = 6
    static let chipRadius: CGFloat = 4
    static let contentMaxWidth: CGFloat = 1240
}

extension Color {
    init(hex: UInt32, alpha: Double = 1.0) {
        self.init(
            .sRGB,
            red:   Double((hex >> 16) & 0xFF) / 255.0,
            green: Double((hex >> 8) & 0xFF) / 255.0,
            blue:  Double(hex & 0xFF) / 255.0,
            opacity: alpha)
    }
}

extension View {
    // .card — flat bg2 fill, 1px hairline border, SQUARE, no shadow, no gradient.
    func card(padding: CGFloat = 22) -> some View {
        self
            .padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Rectangle().fill(PX.bg2))
            .overlay(Rectangle().strokeBorder(PX.line, lineWidth: 1))
    }

    // A nested/hover surface (bg3) with a hairline — square by default.
    func inset(padding: CGFloat = 14, radius: CGFloat = 0,
               fill: Color = PX.bg3, stroke: Color = PX.line) -> some View {
        self
            .padding(padding)
            .background(RoundedRectangle(cornerRadius: radius).fill(fill))
            .overlay(RoundedRectangle(cornerRadius: radius).strokeBorder(stroke, lineWidth: 1))
    }
}

// The uppercase section label used on card headers (.dash-card-label).
struct CardLabel: View {
    let text: String
    var body: some View {
        Text(text.uppercased())
            .font(.system(size: 11, weight: .semibold))
            .tracking(0.6)
            .foregroundStyle(PX.muted)
    }
}

// Page H1 (.card h1) — 24px, tight tracking.
struct PageTitle: View {
    let text: String
    var subtitle: String? = nil
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(text).font(.system(size: 24, weight: .semibold)).tracking(-0.2).foregroundStyle(PX.text)
            if let subtitle {
                Text(subtitle).font(.system(size: 14)).foregroundStyle(PX.text2)
            }
        }
    }
}
