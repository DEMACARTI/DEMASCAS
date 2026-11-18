// DEMASCAS.app — runs DEMASCAS on its own, no Terminal.
//
// Start it once (or let it open at login); it starts the whole stack
// (model server, tool server, voice daemon) in the background, restarts it if
// it crashes, and stops it cleanly on Quit.  Because DEMASCAS no longer lives
// inside Terminal, closing Terminal can't shut it down.
//
// macOS asks for Microphone / Accessibility / Screen Recording for
// "DEMASCAS" (this app), not for Terminal.

import AppKit
import ApplicationServices
import AVFoundation
import ServiceManagement

// MARK: - the DEMASCAS process group

final class Stack {
    let root: String
    let logPath: String
    private(set) var pid: pid_t = 0
    private var wantRunning = false
    private var restarts: [Date] = []
    var onChange: ((String) -> Void)?
    private(set) var state = "Stopped" { didSet { let s = state; DispatchQueue.main.async { self.onChange?(s) } } }

    init(root: String) {
        self.root = root
        let logs = (NSHomeDirectory() as NSString).appendingPathComponent(".demascas/logs")
        try? FileManager.default.createDirectory(atPath: logs, withIntermediateDirectories: true)
        logPath = (logs as NSString).appendingPathComponent("demascas.log")
    }

    var running: Bool { pid > 0 && kill(pid, 0) == 0 }

    func start() {
        guard !running else { return }
        wantRunning = true
        rotateLog()
        let fd = open(logPath, O_WRONLY | O_CREAT | O_APPEND, 0o644)
        let banner = "\n──── DEMASCAS started by the app \(Date()) ────\n"
        _ = banner.withCString { write(fd, $0, strlen($0)) }

        var actions: posix_spawn_file_actions_t? = nil
        posix_spawn_file_actions_init(&actions)
        posix_spawn_file_actions_addopen(&actions, 0, "/dev/null", O_RDONLY, 0)
        posix_spawn_file_actions_adddup2(&actions, fd, 1)
        posix_spawn_file_actions_adddup2(&actions, fd, 2)
        posix_spawn_file_actions_addchdir_np(&actions, root)
        var attr: posix_spawnattr_t? = nil
        posix_spawnattr_init(&attr)
        // its own process group: Quit stops EVERYTHING it started, like Ctrl-C did
        posix_spawnattr_setflags(&attr, Int16(POSIX_SPAWN_SETPGROUP))
        posix_spawnattr_setpgroup(&attr, 0)

        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" + (env["PATH"] ?? "")
        env["DEMASCAS_APP"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        let envp: [UnsafeMutablePointer<CChar>?] = env.map { strdup("\($0.key)=\($0.value)") } + [nil]
        let args = ["/bin/bash", "-l", "\(root)/scripts/run.sh"]
        let argv: [UnsafeMutablePointer<CChar>?] = args.map { strdup($0) } + [nil]
        var child: pid_t = 0
        let rc = posix_spawn(&child, "/bin/bash", &actions, &attr, argv, envp)
        argv.forEach { free($0) }
        envp.forEach { free($0) }
        posix_spawn_file_actions_destroy(&actions)
        posix_spawnattr_destroy(&attr)
        close(fd)
        guard rc == 0 else { state = "Couldn't start (error \(rc))"; return }
        pid = child
        state = "Running"
        DispatchQueue.global().async { [weak self] in
            var status: Int32 = 0
            waitpid(child, &status, 0)
            DispatchQueue.main.async { self?.exited(child) }
        }
    }

    private func exited(_ child: pid_t) {
        guard child == pid else { return }
        pid = 0
        guard wantRunning else { state = "Stopped"; return }
        // crashed — try again, but not forever
        restarts = restarts.filter { $0.timeIntervalSinceNow > -300 }
        if restarts.count < 3 {
            restarts.append(Date())
            state = "Restarting…"
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) { self.start() }
        } else {
            wantRunning = false
            state = "Stopped — it crashed 3 times (see the log)"
        }
    }

    func stop(then: (() -> Void)? = nil) {
        wantRunning = false
        let p = pid
        guard p > 0 else { then?(); return }
        state = "Stopping…"
        kill(-p, SIGINT)                       // the whole group, like Ctrl-C
        DispatchQueue.global().async {
            let deadline = Date().addingTimeInterval(15)
            while kill(p, 0) == 0 && Date() < deadline { usleep(200_000) }
            if kill(p, 0) == 0 { kill(-p, SIGKILL) }
            DispatchQueue.main.async { self.pid = 0; self.state = "Stopped"; then?() }
        }
    }

    private func rotateLog() {
        if let a = try? FileManager.default.attributesOfItem(atPath: logPath),
           let size = a[.size] as? Int, size > 20_000_000 {
            let old = logPath + ".1"
            try? FileManager.default.removeItem(atPath: old)
            try? FileManager.default.moveItem(atPath: logPath, toPath: old)
        }
    }
}

// MARK: - menu bar

final class AppDelegate: NSObject, NSApplicationDelegate {
    var item: NSStatusItem!
    var stack: Stack!
    var noir: NoirWindow!
    let stateItem = NSMenuItem(title: "Starting…", action: nil, keyEquivalent: "")
    let toggleItem = NSMenuItem(title: "Stop", action: #selector(toggle), keyEquivalent: "")
    let loginItem = NSMenuItem(title: "Open at Login", action: #selector(toggleLogin), keyEquivalent: "")

    func applicationDidFinishLaunching(_ n: Notification) {
        let root = Bundle.main.object(forInfoDictionaryKey: "DEMASCASRoot") as? String ?? ""
        stack = Stack(root: root)
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.image = NSImage(systemSymbolName: "waveform.circle", accessibilityDescription: "DEMASCAS")
        let menu = NSMenu()
        stateItem.isEnabled = false
        menu.addItem(stateItem)
        menu.addItem(.separator())
        toggleItem.target = self
        menu.addItem(toggleItem)
        menu.addItem(withTitle: "Show DEMASCAS", action: #selector(showWindow), keyEquivalent: "d").target = self
        menu.addItem(withTitle: "Restart", action: #selector(restart), keyEquivalent: "r").target = self
        menu.addItem(withTitle: "Show Log", action: #selector(showLog), keyEquivalent: "l").target = self
        menu.addItem(withTitle: "Permissions…", action: #selector(showPermissions), keyEquivalent: "p").target = self
        loginItem.target = self
        menu.addItem(loginItem)
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit DEMASCAS", action: #selector(quit), keyEquivalent: "q").target = self
        item.menu = menu
        stack.onChange = { [weak self] s in self?.refresh(s) }
        Permissions.requestAll()
        noir = NoirWindow(logPath: stack.logPath)
        noir.show()
        refreshLogin()
        if !FileManager.default.fileExists(atPath: root + "/scripts/run.sh") {
            refresh("Can't find DEMASCAS at \(root)")
            return
        }
        stack.start()
    }

    func refresh(_ s: String) {
        stateItem.title = "DEMASCAS — \(s)"
        toggleItem.title = stack.running ? "Stop" : "Start"
        let symbol = s == "Running" ? "waveform.circle.fill" : "waveform.circle"
        item.button?.image = NSImage(systemSymbolName: symbol, accessibilityDescription: "DEMASCAS")
    }

    @objc func toggle() { stack.running ? stack.stop() : stack.start() }
    @objc func restart() { stack.stop { self.stack.start() } }
    @objc func showWindow() { noir.toggle() }
    @objc func showPermissions() { Permissions.showSheet(restart: { [weak self] in self?.restart() }) }
    @objc func showLog() { NSWorkspace.shared.open(URL(fileURLWithPath: stack.logPath)) }

    @objc func toggleLogin() {
        let svc = SMAppService.mainApp
        do {
            if svc.status == .enabled { try svc.unregister() } else { try svc.register() }
        } catch {
            let a = NSAlert()
            a.messageText = "Couldn't change Open at Login"
            a.informativeText = "\(error.localizedDescription)\nYou can add DEMASCAS in System Settings → General → Login Items."
            a.runModal()
        }
        refreshLogin()
    }

    func refreshLogin() { loginItem.state = SMAppService.mainApp.status == .enabled ? .on : .off }

    @objc func quit() { stack.stop { NSApp.terminate(nil) } }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if stack.running {
            stack.stop { NSApp.reply(toApplicationShouldTerminate: true) }
            return .terminateLater
        }
        return .terminateNow
    }
}

// one copy only
if NSRunningApplication.runningApplications(withBundleIdentifier: Bundle.main.bundleIdentifier ?? "").count > 1 {
    exit(0)
}
let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)      // menu bar only, no Dock icon
app.run()


// MARK: - permissions
//
// macOS lists an app under Accessibility / Screen Recording only after THE APP
// asks.  DEMASCAS's helpers (Python, the voice daemon, osascript, screencapture)
// run as this app's children, so what's granted here covers them too.

enum Permissions {
    static var accessibility: Bool { AXIsProcessTrusted() }
    static var screen: Bool { CGPreflightScreenCaptureAccess() }
    static var microphone: Bool { AVCaptureDevice.authorizationStatus(for: .audio) == .authorized }

    static func requestAll() {
        if !accessibility {
            let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
            _ = AXIsProcessTrustedWithOptions([key: true] as CFDictionary)
        }
        if !screen { _ = CGRequestScreenCaptureAccess() }
        if AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined {
            AVCaptureDevice.requestAccess(for: .audio) { _ in }
        }
    }

    static func open(_ anchor: String) {
        if let u = URL(string: "x-apple.systempreferences:com.apple.preference.security?\(anchor)") { NSWorkspace.shared.open(u) }
    }

    static func showSheet(restart: @escaping () -> Void) {
        let rows: [(String, Bool, String)] = [
            ("Microphone — hearing you", microphone, "Privacy_Microphone"),
            ("Accessibility — clicking, typing, reading windows", accessibility, "Privacy_Accessibility"),
            ("Screen Recording — seeing the screen", screen, "Privacy_ScreenCapture"),
        ]
        let a = NSAlert()
        a.messageText = "DEMASCAS permissions"
        a.informativeText = rows.map { "\($0.1 ? "✓" : "✗")  \($0.0)" }.joined(separator: "\n")
            + "\n\nTurn DEMASCAS on in each list that shows ✗. Screen Recording takes effect after a restart of DEMASCAS."
        let missing = rows.first { !$0.1 }
        if let m = missing { a.addButton(withTitle: "Open \(m.0.components(separatedBy: " —").first ?? "") settings") }
        a.addButton(withTitle: "Restart DEMASCAS")
        a.addButton(withTitle: "Close")
        NSApp.activate(ignoringOtherApps: true)
        let r = a.runModal()
        if let m = missing, r == .alertFirstButtonReturn { requestAll(); open(m.2) }
        else if (missing == nil && r == .alertFirstButtonReturn) || (missing != nil && r == .alertSecondButtonReturn) { restart() }
    }
}
