// Spike authority daemon. Runs as _ciao under a root LaunchDaemon.
// Listens on a Unix socket, authenticates every peer by LOCAL_PEERCRED + audit-token code
// signing requirement, and appends HMAC-signed events to state it alone owns.
import Foundation
import Security
import CryptoKit

let SOL_LOCAL_: Int32 = 0
let LOCAL_PEERCRED_: Int32 = 1
let LOCAL_PEERPID_: Int32 = 2
let LOCAL_PEERTOKEN_: Int32 = 6

let args = CommandLine.arguments
let sockPath = args.count > 1 ? args[1] : "/var/ciao-authority/run/authority.sock"
let stateDir = args.count > 2 ? args[2] : "/var/ciao-authority/state"
let fm = FileManager.default
try? fm.createDirectory(atPath: stateDir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])

let keyPath = stateDir + "/key"
if !fm.fileExists(atPath: keyPath) {
    var k = [UInt8](repeating: 0, count: 32)
    _ = SecRandomCopyBytes(kSecRandomDefault, 32, &k)
    fm.createFile(atPath: keyPath, contents: Data(k), attributes: [.posixPermissions: 0o600])
}
let key = SymmetricKey(data: (try? Data(contentsOf: URL(fileURLWithPath: keyPath))) ?? Data())

func hex(_ d: Data) -> String { d.map { String(format: "%02x", $0) }.joined() }
func audit(_ s: String) {
    let line = "\(Date()) \(s)\n"
    if let h = FileHandle(forWritingAtPath: stateDir + "/audit.log") { h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); h.closeFile() }
    else { fm.createFile(atPath: stateDir + "/audit.log", contents: line.data(using: .utf8)) }
}
func append(_ file: String, _ line: String) {
    let p = stateDir + "/" + file
    if let h = FileHandle(forWritingAtPath: p) { h.seekToEndOfFile(); h.write((line + "\n").data(using: .utf8)!); h.closeFile() }
    else { fm.createFile(atPath: p, contents: (line + "\n").data(using: .utf8), attributes: [.posixPermissions: 0o600]) }
}

// Pinned requirement is read from a root-owned file next to the binary at every connection,
// so an installer/upgrade can change it without touching the daemon.
let pinPath = args.count > 3 ? args[3] : "/Library/PrivilegedHelperTools/com.ciao.spike.authority.req"
func pinned() -> String { ((try? String(contentsOfFile: pinPath)) ?? "identifier \"none\"").trimmingCharacters(in: .whitespacesAndNewlines) }

struct Peer { var uid: UInt32 = 0; var pid: Int32 = 0; var ident = "?"; var cdhash = "?"; var flags: UInt32 = 0
    var idReq = false; var pinReq = false; var signed = false; var dbg = "" }

func satisfies(_ code: SecCode, _ req: String) -> Bool {
    var r: SecRequirement?
    guard SecRequirementCreateWithString(req as CFString, [], &r) == errSecSuccess, let rr = r else { return false }
    return SecCodeCheckValidity(code, [], rr) == errSecSuccess
}

func peerInfo(_ fd: Int32) -> Peer {
    var p = Peer()
    var cred = xucred(); var len = socklen_t(MemoryLayout<xucred>.size)
    if getsockopt(fd, SOL_LOCAL_, LOCAL_PEERCRED_, &cred, &len) == 0 { p.uid = cred.cr_uid }
    var pid: Int32 = 0; len = socklen_t(MemoryLayout<Int32>.size)
    if getsockopt(fd, SOL_LOCAL_, LOCAL_PEERPID_, &pid, &len) == 0 { p.pid = pid }
    var tok = audit_token_t(); len = socklen_t(MemoryLayout<audit_token_t>.size)
    guard getsockopt(fd, SOL_LOCAL_, LOCAL_PEERTOKEN_, &tok, &len) == 0 else { p.dbg = "peertoken errno=\(errno)"; return p }
    let tokData = Data(bytes: &tok, count: MemoryLayout<audit_token_t>.size)
    var code: SecCode?
    let gst = SecCodeCopyGuestWithAttributes(nil, [kSecGuestAttributeAudit: tokData] as CFDictionary, [], &code)
    guard gst == errSecSuccess, let c = code else { p.dbg = "guest status=\(gst)"; return p }
    p.idReq = satisfies(c, "identifier \"com.ciao.spike.app\"")
    p.pinReq = satisfies(c, pinned())
    var info: CFDictionary?
    var st: SecStaticCode?
    if SecCodeCopyStaticCode(c, [], &st) == errSecSuccess, let s = st,
       SecCodeCopySigningInformation(s, SecCSFlags(rawValue: kSecCSSigningInformation), &info) == errSecSuccess, let d = info as? [String: Any] {
        p.signed = true
        p.ident = (d[kSecCodeInfoIdentifier as String] as? String) ?? "?"
        p.cdhash = hex((d[kSecCodeInfoUnique as String] as? Data) ?? Data())
        p.flags = (d[kSecCodeInfoFlags as String] as? UInt32) ?? 0
    }
    return p
}

unlink(sockPath)
let s = socket(AF_UNIX, SOCK_STREAM, 0)
var addr = sockaddr_un(); addr.sun_family = sa_family_t(AF_UNIX)
withUnsafeMutablePointer(to: &addr.sun_path) { $0.withMemoryRebound(to: CChar.self, capacity: 104) { _ = strncpy($0, sockPath, 103) } }
let bindRes = withUnsafePointer(to: &addr) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(s, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) } }
guard bindRes == 0 else { audit("bind failed errno=\(errno)"); exit(1) }
chmod(sockPath, 0o666)   // worst case: any local process may connect; identity is checked per message
listen(s, 16)
audit("listening uid=\(getuid()) pid=\(getpid()) sock=\(sockPath)")

while true {
    let c = accept(s, nil, nil); if c < 0 { continue }
    var buf = [UInt8](repeating: 0, count: 4096)
    let n = read(c, &buf, 4095)
    let msg = n > 0 ? (String(bytes: buf[0..<n], encoding: .utf8) ?? "").trimmingCharacters(in: .whitespacesAndNewlines) : ""
    let p = peerInfo(c)
    let who = "uid=\(p.uid) pid=\(p.pid) id=\(p.ident) cdhash=\(p.cdhash) flags=0x\(String(p.flags, radix: 16)) runtime=\(p.flags & 0x10000 != 0) idReq=\(p.idReq) pinReq=\(p.pinReq) dbg=\(p.dbg)"
    var reply: String
    if msg == "WHO" { reply = who }
    else if msg.hasPrefix("SUBMIT ") {
        let text = String(msg.dropFirst(7))
        if p.pinReq {
            let mac = hex(Data(HMAC<SHA256>.authenticationCode(for: Data(text.utf8), using: key)))
            append("events.log", "\(text)\t\(mac)")
            reply = "ACCEPTED \(mac.prefix(16))"
        } else { reply = "DENIED (pinned requirement not satisfied)" }
    } else if msg == "STATUS" {
        let ev = (try? String(contentsOfFile: stateDir + "/events.log")) ?? ""
        reply = "events=\(ev.split(separator: "\n").count) keyfp=\(hex(Data(SHA256.hash(data: (try? Data(contentsOf: URL(fileURLWithPath: keyPath))) ?? Data())).prefix(4))) pid=\(getpid()) uid=\(getuid())"
    } else { reply = "ERR unknown" }
    audit("\(msg.prefix(60)) :: \(who) :: \(reply)")
    _ = reply.withCString { write(c, $0, strlen($0)) }
    close(c)
}
