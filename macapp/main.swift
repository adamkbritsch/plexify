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
        ensureMount()
        NSWorkspace.shared.notificationCenter.addObserver(
            self, selector: #selector(onWake),
            name: NSWorkspace.didWakeNotification, object: nil)
        startEngine()
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

    // Keep the NAS library mounted over SMB — it drops on sleep. On launch + on wake,
    // remount via Finder using the Keychain-saved credential if it's gone.
    // Set PLEXIFY_SMB_URL / PLEXIFY_SMB_MOUNT (env, or in your LaunchAgent plist) to your NAS.
    let smbURL = ProcessInfo.processInfo.environment["PLEXIFY_SMB_URL"] ?? "smb://your-nas.local/Music"
    let mountPoint = ProcessInfo.processInfo.environment["PLEXIFY_SMB_MOUNT"] ?? "/Volumes/Music"

    func ensureMount() {
        // Only attempt an SMB mount when one is explicitly configured — otherwise a fresh
        // install (or a non-split setup) would spam macOS "can't connect" dialogs.
        guard ProcessInfo.processInfo.environment["PLEXIFY_SMB_URL"] != nil else { return }
        if FileManager.default.fileExists(atPath: mountPoint + "/plexify-music") { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        p.arguments = [smbURL]   // Finder mounts via the Keychain-saved credential (no prompt)
        try? p.run()
    }

    @objc func onWake() {
        ensureMount()
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
