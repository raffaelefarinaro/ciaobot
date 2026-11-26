// XPC variant: Mach service in a LaunchDaemon running as _ciao (or root). The peer requirement is
// enforced by libxpc itself via xpc_connection_set_peer_code_signing_requirement.
import Foundation
import XPC

let svc = "com.ciao.spike.xpc"
let reqPath = "/Library/PrivilegedHelperTools/com.ciao.spike.authority.req"
let listener = xpc_connection_create_mach_service(svc, nil, UInt64(XPC_CONNECTION_MACH_SERVICE_LISTENER))
xpc_connection_set_event_handler(listener) { peer in
    guard xpc_get_type(peer) == XPC_TYPE_CONNECTION else { return }
    let req = ((try? String(contentsOfFile: reqPath)) ?? "identifier \"none\"").trimmingCharacters(in: .whitespacesAndNewlines)
    let rc = xpc_connection_set_peer_code_signing_requirement(peer, req)
    let log = "peer pid=\(xpc_connection_get_pid(peer)) euid=\(xpc_connection_get_euid(peer)) setreq=\(rc)\n"
    if let h = FileHandle(forWritingAtPath: "/tmp/xpcd.log") { h.seekToEndOfFile(); h.write(log.data(using: .utf8)!) } else { try? log.write(toFile: "/tmp/xpcd.log", atomically: true, encoding: .utf8) }
    xpc_connection_set_event_handler(peer) { msg in
        guard xpc_get_type(msg) == XPC_TYPE_DICTIONARY, let reply = xpc_dictionary_create_reply(msg) else { return }
        let text = xpc_dictionary_get_string(msg, "text").map { String(cString: $0) } ?? ""
        xpc_dictionary_set_string(reply, "reply", "XPC-ACCEPTED uid=\(getuid()) peer_euid=\(xpc_connection_get_euid(peer)) text=\(text)")
        xpc_connection_send_message(peer, reply)
    }
    xpc_connection_resume(peer)
}
xpc_connection_resume(listener)
dispatchMain()
