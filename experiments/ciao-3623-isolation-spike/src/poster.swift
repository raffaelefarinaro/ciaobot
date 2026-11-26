// Synthetic input probe: post keystrokes via CGEvent and report TCC state.
import Cocoa
import ApplicationServices
print("AXIsProcessTrusted=\(AXIsProcessTrusted()) PostEventAccess=\(CGPreflightPostEventAccess()) ListenEventAccess=\(CGPreflightListenEventAccess())")
let text = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "forged"
let src = CGEventSource(stateID: .hidSystemState)
for ch in text.utf16 {
    var c = ch
    for down in [true, false] {
        let e = CGEvent(keyboardEventSource: src, virtualKey: 0, keyDown: down)!
        e.keyboardSetUnicodeString(stringLength: 1, unicodeString: &c)
        e.post(tap: .cghidEventTap); usleep(20000)
    }
}
for down in [true, false] { let e = CGEvent(keyboardEventSource: src, virtualKey: 36, keyDown: down)!; e.post(tap: .cghidEventTap); usleep(20000) }
print("posted")
