// The DEMASCAS window — Terminal Noir.  Live: transcript from DEMASCAS's own
// log, VU meters from the microphone level, YES / NO / STOP buttons sent to
// the daemon (token-protected, localhost only).

import AppKit
import AVFoundation
import Network
import WebKit

final class NoirWindow: NSObject, WKScriptMessageHandler, WKNavigationDelegate {
    let panel: NSPanel
    let web: WKWebView
    private let logPath: String
    private var offset: UInt64 = 0
    private var partial = ""
    private var timer: Timer?
    private var engine: AVAudioEngine?
    private var lastLevelSent = Date.distantPast
    private var ready = false
    private var queued: [String] = []
    private var brain = "…", eyes = "asleep", swap = "…", first = "…"

    init(logPath: String) {
        self.logPath = logPath
        let cfg = WKWebViewConfiguration()
        let ucc = WKUserContentController()
        cfg.userContentController = ucc
        web = WKWebView(frame: NSRect(x: 0, y: 0, width: 880, height: 600), configuration: cfg)
        web.setValue(false, forKey: "drawsBackground")
        panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 880, height: 600),
                        styleMask: [.titled, .closable, .resizable, .fullSizeContentView, .nonactivatingPanel],
                        backing: .buffered, defer: false)
        super.init()
        ucc.add(self, name: "dmx")
        web.navigationDelegate = self
        panel.titlebarAppearsTransparent = true
        panel.titleVisibility = .hidden
        panel.isMovableByWindowBackground = true
        panel.level = .floating
        panel.backgroundColor = NSColor(red: 0.03, green: 0.016, blue: 0, alpha: 1)
        panel.minSize = NSSize(width: 640, height: 440)
        panel.contentView = web
        panel.center()
        panel.setFrameAutosaveName("DEMASCASNoir")
        panel.hidesOnDeactivate = false
        if let url = Bundle.main.url(forResource: "noir", withExtension: "html") {
            web.loadFileURL(url, allowingReadAccessTo: url.deletingLastPathComponent())
        }
        // start reading from the end: only what happens from now on
        if let a = try? FileManager.default.attributesOfItem(atPath: logPath), let size = a[.size] as? UInt64 { offset = size }
        timer = Timer.scheduledTimer(withTimeInterval: 0.15, repeats: true) { [weak self] _ in self?.tail() }
        Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in self?.refreshSwap() }
        refreshSwap()
        startMic()
    }

    func show() { panel.orderFrontRegardless() }
    func toggle() { panel.isVisible ? panel.orderOut(nil) : show() }

    // MARK: JS bridge
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        ready = true
        queued.forEach { web.evaluateJavaScript($0) }
        queued.removeAll()
        pushStatus()
        js("dmx.state('LISTENING','#5cffb0')")
    }

    private func js(_ s: String) {
        if ready { web.evaluateJavaScript(s) } else { queued.append(s) }
    }

    private func q(_ s: String) -> String {
        let data = try? JSONSerialization.data(withJSONObject: [s])
        let arr = data.flatMap { String(data: $0, encoding: .utf8) } ?? "[\"\"]"
        return String(arr.dropFirst().dropLast())       // a JSON string literal
    }

    func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let cmd = body["cmd"] as? String,
              ["yes", "no", "stop"].contains(cmd) else { return }
        sendControl(cmd)
        if cmd != "stop" { js("dmx.confirm(null)") }
    }

    private func sendControl(_ cmd: String) {
        let tokenPath = (NSHomeDirectory() as NSString).appendingPathComponent(".demascas/app_token")
        guard let token = try? String(contentsOfFile: tokenPath, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines),
              !token.isEmpty else { return }
        let conn = NWConnection(host: "127.0.0.1", port: 5002, using: .tcp)
        conn.stateUpdateHandler = { state in
            if case .ready = state {
                conn.send(content: "\(token) \(cmd)\n".data(using: .utf8), completion: .contentProcessed { _ in conn.cancel() })
            }
        }
        conn.start(queue: .global())
    }

    // MARK: log → transcript
    private func tail() {
        guard let h = FileHandle(forReadingAtPath: logPath) else { return }
        defer { try? h.close() }
        let size = (try? h.seekToEnd()) ?? 0
        if size < offset { offset = 0 }                    // log rotated
        guard size > offset else { return }
        try? h.seek(toOffset: offset)
        let data = h.readData(ofLength: Int(min(size - offset, 512_000)))
        offset += UInt64(data.count)
        partial += String(decoding: data, as: UTF8.self)
        var lines = partial.components(separatedBy: "\n")
        partial = lines.removeLast()
        lines.forEach(parse)
    }

    private func after(_ line: String, _ marker: String) -> String? {
        guard let r = line.range(of: marker) else { return nil }
        return String(line[r.upperBound...]).trimmingCharacters(in: .whitespaces)
    }

    private func parse(_ line: String) {
        if line.contains("[🔊 WAKE WORD]") {
            js("dmx.state('AWAKE','#5cffb0')"); js("dmx.line('WAKE', \(q("heard “DEMASCAS”")))")
        } else if var t = after(line, "YOU SAID]:") {
            if let r = t.range(of: "\" (confidence") { t = String(t[..<r.lowerBound]) }
            js("dmx.line('YOU', \(q(t.trimmingCharacters(in: CharacterSet(charactersIn: "\"")))))"); js("dmx.confirm(null)")
        } else if let t = after(line, "[🗣 DEMASCAS]:") {
            if t == "Standing by." { js("dmx.state('ASLEEP','#a78bff')") }
            js("dmx.line('DEMASCAS', \(q(t)))")
        } else if let t = after(line, "[CONFIRM] waiting for yes/no:") {
            js("dmx.line('HOLD', \(q(t)))"); js("dmx.confirm(\(q(t)))")
        } else if let t = after(line, "[🖱 APP]") {
            js("dmx.line('YOU', \(q(t + " (button)")))"); js("dmx.confirm(null)")
        } else if line.contains("[TOOL CALL"), let t = after(line, "]") {
            let name = t.components(separatedBy: "(").first ?? t
            js("dmx.line('ACT', \(q(name.replacingOccurrences(of: "_", with: " "))))")
        } else if let t = after(line, "[🔧 TASK]") {
            js("dmx.task(true)"); js("dmx.state('WORKING','#ffd000')")
            js("dmx.line('TASK', \(q(t.components(separatedBy: " — ").last ?? t)))")
        } else if line.contains("[✅ TASK DONE]") {
            js("dmx.task(false)"); js("dmx.state('LISTENING','#5cffb0')")
        } else if line.contains("going back to sleep") {
            js("dmx.state('ASLEEP','#a78bff')"); js("dmx.task(false)")
        } else if line.contains("[LLM]") {
            if let r = line.range(of: "first word "), let s = line[r.upperBound...].split(separator: " ").first { first = "\(s)s" }
            pushStatus()
        } else if let t = after(line, "[✓] Model:") {
            brain = t.components(separatedBy: " via ").first?.components(separatedBy: "/").last ?? brain; pushStatus()
        } else if line.contains("[VISION] ready") {
            eyes = "awake"; pushStatus()
        } else if line.contains("[VISION] unloaded") {
            eyes = "asleep"; pushStatus()
        } else if line.contains("[⏹ CANCEL]") || line.contains("[⏹ QUIET]") {
            js("dmx.task(false)"); js("dmx.line('INFO', 'stopped')")
        }
    }

    private func refreshSwap() {
        var usage = xsw_usage()
        var size = MemoryLayout<xsw_usage>.size
        if sysctlbyname("vm.swapusage", &usage, &size, nil, 0) == 0 {
            swap = String(format: "%.1fG", Double(usage.xsu_used) / 1_073_741_824)
            pushStatus()
        }
    }

    private func pushStatus() {
        js("dmx.status(\(q("BRAIN \(brain) · EYES \(eyes) · SWAP \(swap) · 1ST WORD \(first)")))")
    }

    // MARK: microphone level → VU meters
    private func startMic() {
        AVCaptureDevice.requestAccess(for: .audio) { [weak self] ok in
            guard ok else { return }
            DispatchQueue.main.async { self?.runEngine() }
        }
    }

    private func runEngine() {
        let e = AVAudioEngine()
        let input = e.inputNode
        let fmt = input.outputFormat(forBus: 0)
        guard fmt.channelCount > 0 else { return }
        input.installTap(onBus: 0, bufferSize: 1024, format: fmt) { [weak self] buf, _ in
            guard let self, let ch = buf.floatChannelData?[0] else { return }
            let n = Int(buf.frameLength)
            var sum: Float = 0
            for i in 0..<n { sum += ch[i] * ch[i] }
            let rms = sqrt(sum / Float(max(n, 1)))
            let db = 20 * log10(max(rms, 1e-6))                 // ≈ -60 … 0
            let level = max(0, min(1, (db + 60) / 60))
            let now = Date()
            if now.timeIntervalSince(self.lastLevelSent) > 0.05 {
                self.lastLevelSent = now
                DispatchQueue.main.async { if self.panel.isVisible { self.js(String(format: "dmx.level(%.3f)", level)) } }
            }
        }
        try? e.start()
        engine = e
    }
}
