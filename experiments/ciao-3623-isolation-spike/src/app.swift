// Spike "operator app". CLI modes for harness use; "gui" is the composer prototype.
// The real app must expose no CLI/URL/AppleScript input surface; the CLI exists here
// only so the harness can show what a copied/driven binary can do.
import Cocoa
import Foundation
import XPC

let sockPath = ProcessInfo.processInfo.environment["SPIKE_SOCK"] ?? "/var/ciao-authority/run/authority.sock"

func ask(_ msg: String) -> String {
    let s = socket(AF_UNIX, SOCK_STREAM, 0); defer { close(s) }
    var addr = sockaddr_un(); addr.sun_family = sa_family_t(AF_UNIX)
    withUnsafeMutablePointer(to: &addr.sun_path) { $0.withMemoryRebound(to: CChar.self, capacity: 104) { _ = strncpy($0, sockPath, 103) } }
    let r = withUnsafePointer(to: &addr) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(s, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) } }
    if r != 0 { return "CONNECT_FAILED errno=\(errno) \(String(cString: strerror(errno)))" }
    _ = msg.withCString { write(s, $0, strlen($0)) }
    var buf = [UInt8](repeating: 0, count: 4096)
    let n = read(s, &buf, 4095)
    return n > 0 ? String(bytes: buf[0..<n], encoding: .utf8) ?? "" : "EMPTY"
}

class Delegate: NSObject, NSApplicationDelegate, NSTextFieldDelegate {
    var win: NSWindow!; var field: NSTextField!
    func applicationDidFinishLaunching(_ n: Notification) {
        win = NSWindow(contentRect: NSRect(x: 100, y: 300, width: 480, height: 80), styleMask: [.titled], backing: .buffered, defer: false)
        field = NSTextField(frame: NSRect(x: 10, y: 20, width: 460, height: 30))
        field.target = self; field.action = #selector(send(_:))
        win.contentView!.addSubview(field); win.title = "composer"
        win.makeKeyAndOrderFront(nil); win.makeFirstResponder(field)
        NSApp.activate(ignoringOtherApps: true)
        try? "up\n".write(toFile: NSHomeDirectory() + "/composer.up", atomically: true, encoding: .utf8)
    }
    @objc func send(_ s: NSTextField) {
        let reply = ask("SUBMIT " + s.stringValue)
        let line = "typed=\(s.stringValue) -> \(reply)\n"
        if let h = FileHandle(forWritingAtPath: NSHomeDirectory() + "/composer.log") { h.seekToEndOfFile(); h.write(line.data(using: .utf8)!) }
        else { try? line.write(toFile: NSHomeDirectory() + "/composer.log", atomically: true, encoding: .utf8) }
        s.stringValue = ""
    }
}

let a = CommandLine.arguments
switch a.count > 1 ? a[1] : "who" {
case "who": print(ask("WHO"))
case "status": print(ask("STATUS"))
case "submit": print(ask("SUBMIT " + a.dropFirst(2).joined(separator: " ")))
case "sleep": sleep(600)
case "xpc":
    let c = xpc_connection_create_mach_service("com.ciao.spike.xpc", nil, 0)
    xpc_connection_set_event_handler(c) { _ in }
    xpc_connection_resume(c)
    let m = xpc_dictionary_create(nil, nil, 0)
    xpc_dictionary_set_string(m, "text", a.dropFirst(2).joined(separator: " "))
    let r = xpc_connection_send_message_with_reply_sync(c, m)
    if xpc_get_type(r) == XPC_TYPE_DICTIONARY { print(xpc_dictionary_get_string(r, "reply").map { String(cString: $0) } ?? "") }
    else { print("XPC_NO_REPLY (connection invalid/interrupted: \(String(cString: xpc_copy_description(r))))") }
case "gui":
    let app = NSApplication.shared; let d = Delegate(); app.delegate = d
    app.setActivationPolicy(.regular); app.run()
default: print("usage")
}
