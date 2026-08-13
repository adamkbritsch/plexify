import Cocoa
import SwiftUI

// Native macOS shell for Plexify. Launches the bundled Python engine (gunicorn, UI-only role
// for bring-up) and renders a fully NATIVE SwiftUI UI that polls the engine's JSON API — the
// web UI is replicated pixel-for-pixel in SwiftUI (PLEXIFY OLED theme), no WebView.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    var window: NSWindow!
    var engine: Process?
    let store = PlexifyStore()

    // Dev paths (overridable via env for a bundled build later).
    // Dev-build paths — the app launches a local engine. Defaults assume the repo is cloned
    // at ~/plexify-mac; override with the PLEXIFY_* env vars (see docs/MACOS.md).
    var venvGunicorn: String { ProcessInfo.processInfo.environment["PLEXIFY_GUNICORN"] ?? (NSHomeDirectory() + "/plexify-mac/venv/bin/gunicorn") }
    var engineDir: String { ProcessInfo.processInfo.environment["PLEXIFY_ENGINE_DIR"] ?? (NSHomeDirectory() + "/plexify-mac/engine-run") }
    var dataDir: String { ProcessInfo.processInfo.environment["PLEXIFY_DATA_DIR"] ?? (NSHomeDirectory() + "/plexify-mac/data") }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.appearance = NSAppearance(named: .darkAqua)   // dark mode only
        // CLIENT MODE (PLEXIFY_ENGINE_URL set): this app is a front end and nothing else. No
        // engine process, no SMB mount, no scheduled work of any kind — the engine owns all of
        // that on its own host, next to the storage. Everything below this branch exists purely
        // to keep a LOCAL engine alive, so none of it should run when the engine is elsewhere.
        if PlexifyStore.engineIsRemote {
            NSLog("Plexify: client mode — engine at %@", PlexifyStore.engineBase)
        } else {
            ensureMount()
            // Periodic mount self-heal — launch/wake hooks miss network hops (leaving home,
            // VPN flips), and a silently-dead mount corrupts every metric the engine serves.
            Timer.scheduledTimer(withTimeInterval: 90, repeats: true) { [weak self] _ in
                self?.ensureMount()
            }
            startEngine()
        }
        NSWorkspace.shared.notificationCenter.addObserver(
            self, selector: #selector(onWake),
            name: NSWorkspace.didWakeNotification, object: nil)
        buildMenu()

        let rect = NSRect(x: 0, y: 0, width: 1200, height: 820)
        window = NSWindow(contentRect: rect,
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Plexify"
        window.minSize = NSSize(width: 900, height: 640)
        window.center()
        window.setFrameAutosaveName("PlexifyMainWindow")
        window.titlebarAppearsTransparent = true
        window.backgroundColor = NSColor.black

        let root = PlexifyRootView().environmentObject(store)
        let host = NSHostingView(rootView: root)
        host.autoresizingMask = [.width, .height]
        window.contentView = host
        window.delegate = self
        // Launched with --minimized (by the login LaunchAgent): run in the background — the engine
        // and polling stay active, but don't show a window or steal focus. Click the Dock icon to
        // reveal it (applicationShouldHandleReopen orders the window front on demand).
        if !CommandLine.arguments.contains("--minimized") {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        }

        store.start()
    }

    // Launch and own a LOCAL engine. Only called in local mode — in client mode a second engine
    // would mean two schedulers and two databases racing for one library.
    func startEngine() {
        // kill any stale engine on our port first
        let pk = Process()
        pk.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        pk.arguments = ["-f", "gunicorn.*app.main:app"]
        try? pk.run(); pk.waitUntilExit()

        let p = Process()
        p.executableURL = URL(fileURLWithPath: venvGunicorn)
        p.arguments = ["--bind", "127.0.0.1:8787", "--workers", "1", "--threads", "4",
                       "--timeout", "120", "app.main:app"]
        p.currentDirectoryURL = URL(fileURLWithPath: engineDir)
        var env = ProcessInfo.processInfo.environment
        env["DATA_DIR"] = dataDir
        env["PLEXIFY_START_SCHEDULER"] = "0"   // UI-only during bring-up
        env["PUBLIC_BASE_URL"] = "http://127.0.0.1:8787"
        p.environment = env
        do { try p.run(); engine = p } catch { NSLog("Plexify: engine launch failed: \(error)") }
    }

    // Keep the NAS library mounted over SMB — it drops on sleep AND whenever the Mac
    // changes networks (leaving home kills the LAN path; only the tailnet survives).
    // PLEXIFY_SMB_URL takes a COMMA-SEPARATED list of URLs; every check probes them in
    // order and mounts the first USABLE one, so the fast LAN form is used at home and the
    // Tailscale form takes over away from home — within one 90s tick, not one form per
    // tick. Checked on launch, on wake, and every 90s (a dead mount makes the whole
    // engine lie: empty feeds, zero pending counts, failed imports).
    let smbURLs = (ProcessInfo.processInfo.environment["PLEXIFY_SMB_URL"] ?? "smb://your-nas.local/Music")
        .split(separator: ",").map { String($0).trimmingCharacters(in: .whitespaces) }
    nonisolated let mountPoint = ProcessInfo.processInfo.environment["PLEXIFY_SMB_MOUNT"] ?? "/Volumes/Music"
    // Set only while a mount attempt is in flight — the 90s timer must not stack attempts
    // on top of a mount that is simply still negotiating.
    var mounting = false
    // url -> unix time until which that URL is skipped after a failed mount.
    nonisolated(unsafe) var mountBackoff: [String: Double] = [:]

    func ensureMount() {
        // Only attempt an SMB mount when one is explicitly configured — otherwise a fresh
        // install (or a non-split setup) would try to reach a NAS that isn't there.
        guard ProcessInfo.processInfo.environment["PLEXIFY_SMB_URL"] != nil else { return }
        if FileManager.default.fileExists(atPath: mountPoint + "/plexify-music") { return }
        if mounting { return }
        mounting = true
        let urls = smbURLs
        // Everything below runs OFF the main thread: the reachability probe blocks for up to
        // 1.5s per address and the mount itself takes seconds, and a UI app must not stall
        // for that on a 90s timer.
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            defer { DispatchQueue.main.async { self.mounting = false } }
            // Take the first form that answers on 445 and isn't in failure backoff. The TCP
            // probe is what keeps us completely silent while offline — no path to the NAS
            // means no attempt at all, rather than a retry that can only end in an error
            // panel.
            let pass = ProcessInfo.processInfo.environment["PLEXIFY_SMB_PASS"] ?? ""
            let now = Date().timeIntervalSince1970
            guard let url = urls.first(where: {
                (self.mountBackoff[$0] ?? 0) < now && self.nasReachable($0, port: 445)
            }) else { return }
            // MOUNT SILENTLY — never hand the URL to Finder. `open smb://…` delegates to
            // Finder, which puts up "Connect to Server" / "There was a problem connecting"
            // panels and steals focus. AppleScript's `mount volume` drives the same NetAuth
            // machinery directly: it creates the mount point under /Volumes (which is
            // root-owned, so plain mount_smbfs can't) and resolves the password out of the
            // login Keychain, with no interface at any point. PLEXIFY_SMB_PASS supplies the
            // password explicitly instead (fed over stdin, never argv, so it can't leak into
            // `ps`) for a share macOS has no saved credential for.
            var target = url
            if !pass.isEmpty, let at = url.range(of: "@"), let scheme = url.range(of: "://") {
                let user = String(url[scheme.upperBound..<at.lowerBound])
                let esc = pass.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? pass
                target = String(url[..<scheme.upperBound]) + user + ":" + esc + String(url[at.lowerBound...])
            }
            let script = "try\nmount volume \"\(target)\"\nend try\n"
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
            p.arguments = ["-"]                       // read the script from stdin, not argv
            let pipe = Pipe()
            p.standardInput = pipe
            p.standardOutput = FileHandle.nullDevice
            p.standardError = FileHandle.nullDevice
            do {
                try p.run()
                pipe.fileHandleForWriting.write(Data(script.utf8))
                try? pipe.fileHandleForWriting.close()
                // WATCHDOG. A mount that has neither succeeded nor failed inside 45s is waiting
                // on something only a human can answer — a credential panel for a share whose
                // password macOS can't resolve is the realistic case. Cancel the request rather
                // than let it sit on the user's screen, and let the backoff below stop us
                // raising it again every 90 seconds.
                let deadline = Date().addingTimeInterval(45)
                while p.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.5) }
                if p.isRunning {
                    p.terminate()
                    NSLog("Plexify: mount of %@ timed out after 45s — cancelled", url)
                }
            } catch {
                NSLog("Plexify: mount failed to launch: \(error)")
            }
            // Back a failing URL off for 10 minutes so a share that can't mount (wrong
            // credential, share renamed, NAS refusing) is retried occasionally instead of on
            // every 90s tick — and so the next tick moves on to the next URL in the list.
            if FileManager.default.fileExists(atPath: self.mountPoint + "/plexify-music") {
                self.mountBackoff[url] = 0
            } else {
                self.mountBackoff[url] = Date().timeIntervalSince1970 + 600
                NSLog("Plexify: mount of %@ did not take — backing off 10 min", url)
            }
        }
    }

    // Quick TCP reachability probe (1.5s) to a host from an smb:// URL — used to skip futile
    // mounts when there's no path to the NAS. Tries EVERY address the name resolves to: a
    // hardcoded AF_INET socket silently failed on .local names, which mDNS resolves to a
    // link-local IPv6 address, so the LAN form was always judged unreachable and every mount
    // fell through to the (much slower) Tailscale form even at home.
    nonisolated func nasReachable(_ smbURL: String, port: Int) -> Bool {
        guard let host = URL(string: smbURL)?.host else { return false }
        var hints = addrinfo(ai_flags: 0, ai_family: AF_UNSPEC, ai_socktype: SOCK_STREAM,
                             ai_protocol: 0, ai_addrlen: 0, ai_canonname: nil,
                             ai_addr: nil, ai_next: nil)
        var res: UnsafeMutablePointer<addrinfo>?
        guard getaddrinfo(host, String(port), &hints, &res) == 0 else { return false }
        defer { freeaddrinfo(res) }
        var node = res
        while let info = node {
            let sock = socket(info.pointee.ai_family, info.pointee.ai_socktype, info.pointee.ai_protocol)
            if sock >= 0 {
                var tv = timeval(tv_sec: 1, tv_usec: 500_000)
                setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
                let ok = connect(sock, info.pointee.ai_addr, info.pointee.ai_addrlen) == 0
                close(sock)
                if ok { return true }
            }
            node = info.pointee.ai_next
        }
        return false
    }

    @objc func onWake() {
        if !PlexifyStore.engineIsRemote { ensureMount() }
        Task { await store.refreshAll() }
    }

    func buildMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About Plexify", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide Plexify", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Quit Plexify", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let viewItem = NSMenuItem(); main.addItem(viewItem)
        let viewMenu = NSMenu(title: "View")
        let reload = NSMenuItem(title: "Refresh", action: #selector(reloadUI), keyEquivalent: "r")
        reload.target = self; viewMenu.addItem(reload)
        viewItem.submenu = viewMenu

        let winItem = NSMenuItem(); main.addItem(winItem)
        let winMenu = NSMenu(title: "Window")
        winMenu.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        winMenu.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        winItem.submenu = winMenu

        NSApp.mainMenu = main
        NSApp.windowsMenu = winMenu
    }

    @objc func reloadUI() { Task { await store.refreshAll() } }

    // Close (red button / ⌘W) and a plain user ⌘Q both MINIMIZE — the app keeps running in the
    // background (engine + polling stay alive). A system logout/shutdown/restart, or a SIGTERM
    // from a deploy/relaunch, still quits it normally.
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.miniaturize(nil)
        return false
    }
    func applicationShouldTerminate(_ app: NSApplication) -> NSApplication.TerminateReply {
        // Let the SYSTEM through: a logout/shutdown/restart quit event carries a 'why?' attribute.
        // Only a plain user ⌘Q (no quit reason) is converted to minimize.
        if let evt = NSAppleEventManager.shared().currentAppleEvent,
           evt.attributeDescriptor(forKeyword: AEKeyword(0x7768793F)) != nil {   // 'why?'
            return .terminateNow
        }
        window.miniaturize(nil)
        return .terminateCancel
    }
    func applicationWillTerminate(_ notification: Notification) {
        guard !PlexifyStore.engineIsRemote else { return }   // never kill someone else's engine
        engine?.terminate()
        let pk = Process()
        pk.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        pk.arguments = ["-f", "gunicorn.*app.main:app"]
        try? pk.run()
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { window.makeKeyAndOrderFront(nil); window.deminiaturize(nil) }
        return true
    }
}

MainActor.assumeIsolated {
    let app = NSApplication.shared
    let delegate = AppDelegate()
    app.delegate = delegate
    NSApp.setActivationPolicy(.regular)
    objc_setAssociatedObject(app, "delegate", delegate, .OBJC_ASSOCIATION_RETAIN)
    app.run()
}
