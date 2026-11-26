import CiaobotAuthority
import Darwin
import Foundation

// ciao-authority: the chat authority daemon (#3623, slice 2).
//
//   ciao-authority provision [--root DIR]
//   ciao-authority enroll    [--root DIR] --operator-public-key-hex HEX --reader-uid N
//   ciao-authority apply     [--root DIR]            (one signed command as JSON on stdin; offline test harness)
//   ciao-authority serve     [--root DIR] --socket PATH
//
// The default root is /var/ciao-authority (root-owned, mode 0700, so the
// default requires root). Tests pass a temporary --root. There is no launchd
// registration, no installer and no XPC here: those are later slices.

func fail(_ message: String, code: Int32 = 1) -> Never {
    FileHandle.standardError.write(Data("ciao-authority: \(message)\n".utf8))
    exit(code)
}

func option(_ name: String, in args: [String]) -> String? {
    guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
    return args[i + 1]
}

let args = Array(CommandLine.arguments.dropFirst())
guard let command = args.first else {
    fail("usage: ciao-authority provision|enroll|apply|serve [--root DIR] ...", code: 64)
}
let root = option("--root", in: args) ?? AuthorityStore.productionRoot

func printJSON(_ value: JSONValue) {
    FileHandle.standardOutput.write(Data(JSON.canonical(value) + [0x0a]))
}

do {
    switch command {
    case "provision":
        try AuthorityStore.provision(root: root)
        print("provisioned \(root)")
    case "enroll":
        guard let hex = option("--operator-public-key-hex", in: args), let key = Hex.decodeHex(hex),
            let uidText = option("--reader-uid", in: args), let uid = UInt32(uidText)
        else { fail("enroll needs --operator-public-key-hex and --reader-uid", code: 64) }
        try AuthorityStore.enroll(root: root, operatorPublicKeyX963: key, readerUID: uid)
        print("enrolled")
    case "apply":
        let store = try AuthorityStore(root: root)
        let authority = try store.openAuthority()
        let input = FileHandle.standardInput.readDataToEndOfFile()
        guard input.count <= maxRequestBytes * 4, let json = try? JSON.parse(Array(input)),
            let signed = try? SignedCommand(json: json)
        else { fail("bad command document", code: 65) }
        printJSON(authority.run(op: signed.command.kind, signed))
    case "serve":
        guard let socketPath = option("--socket", in: args) else { fail("serve needs --socket", code: 64) }
        let store = try AuthorityStore(root: root)
        guard let enrollment = store.enrollment else { fail("not enrolled: run enroll first") }
        let host: AuthorityHost
        do {
            host = AuthorityHost(.ready(try store.openAuthority()))
        } catch let e as AuthorityError {
            // Closed: report it through health, answer nothing else, repair nothing.
            FileHandle.standardError.write(Data("ciao-authority: closed (\(e.code))\n".utf8))
            host = AuthorityHost(.closed(reason: e.code))
        }
        let server = SocketServer(path: socketPath, allowedUID: uid_t(enrollment.readerUID), handler: host.handle)
        try server.start()
        print("serving \(socketPath)")
        fflush(stdout)
        signal(SIGPIPE, SIG_IGN)
        // The sources must stay alive for the life of the process.
        let sources = [SIGTERM, SIGINT].map { sig -> DispatchSourceSignal in
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            source.setEventHandler {
                server.stop()
                exit(0)
            }
            source.resume()
            return source
        }
        withExtendedLifetime(sources) { dispatchMain() }
    default:
        fail("unknown command \(command)", code: 64)
    }
} catch {
    fail("\(error)")
}
