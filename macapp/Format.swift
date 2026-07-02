import Foundation

// Formatting helpers that mirror the web's JS (fmtSecs, ago, fmtBytes) so labels read identically.

func fmtSecs(_ s: Int?) -> String {
    guard let raw = s else { return "—" }
    let v = max(0, raw)
    if v < 60 { return "\(v)s" }
    let m = v / 60, r = v % 60
    return r == 0 ? "\(m)m" : "\(m)m \(r)s"
}

func fmtBytes(_ n: Int?) -> String {
    let v = Double(n ?? 0)
    guard v > 0 else { return "" }
    if v >= 1_073_741_824 { return String(format: "%.1f GB", v / 1_073_741_824) }
    if v >= 1_048_576 { return "\(Int((v / 1_048_576).rounded())) MB" }
    return "\(Int((v / 1024).rounded())) KB"
}

// Relative "X ago" from an ISO8601 timestamp, matching the reward-feed JS.
func ago(_ iso: String?) -> String {
    guard let date = parseISO(iso) else { return "" }
    let sec = Int(Date().timeIntervalSince(date))
    if sec < 60 { return "\(max(0, sec))s ago" }
    let min = sec / 60
    if min < 60 { return "\(min) min ago" }
    let hr = min / 60
    if hr < 24 { return "\(hr)h ago" }
    let d = hr / 24
    if d == 1 { return "yesterday" }
    if d < 7 { return "\(d) days ago" }
    return "\(d / 7)w ago"
}

private let _isoFrac: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]; return f
}()
private let _isoPlain = ISO8601DateFormatter()

func parseISO(_ iso: String?) -> Date? {
    guard var s = iso else { return nil }
    if !s.hasSuffix("Z") && !s.contains("+") { s += "Z" }   // engine emits naive UTC
    return _isoFrac.date(from: s) ?? _isoPlain.date(from: s)
}

// Deterministic hue (0…359) from artist|album — matches the feed-art gradient JS.
func hueFor(_ artist: String?, _ album: String?) -> Double {
    let s = (artist ?? "") + "|" + (album ?? "")
    var h: UInt32 = 0
    for ch in s.unicodeScalars { h = h &* 31 &+ ch.value }
    return Double(h % 360)
}

// Canonical display name for a download source (SourceTag then uppercases it).
func sourceDisplay(_ s: String) -> String {
    switch s.lowercased() {
    case "soulseek":  return "Soulseek"
    case "squid":     return "squid.wtf"
    case "spotiflac": return "SpotiFLAC"
    case "telegram":  return "Telegram"
    case "sweep":     return "Sweep"
    case "import":    return "Import"
    default:          return s
    }
}

func initials(_ s: String?, max: Int = 3) -> String {
    let words = (s ?? "?").split(whereSeparator: { $0 == " " })
    let letters = words.compactMap { $0.first }.prefix(max)
    return String(letters).uppercased()
}
