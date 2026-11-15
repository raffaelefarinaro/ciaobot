import Darwin
import Foundation

/// Writes one line of the workspace `.env`. The file usually holds secrets, so
/// only the `PWA_PORT` line is rewritten: every other byte is preserved, no
/// content is ever logged, and a failure leaves the file unchanged.
enum WorkspaceDotenvPort {
    enum Failure: Error {
        /// The existing file could not be read, or is not UTF-8 text.
        case unreadable
    }

    /// Replaces the `PWA_PORT` line in place (tolerating the same `export ` and
    /// quote forms the engine's reader does), appends it when there is none,
    /// or creates a file containing only it. The write is atomic; the file's
    /// existing permissions are restored, and a new file is 0600.
    static func setPort(_ port: Int, in dotenv: URL) throws {
        let fileManager = FileManager.default
        let line = "PWA_PORT=\(port)"

        let original: String?
        let permissions: NSNumber?
        if fileManager.fileExists(atPath: dotenv.path) {
            guard let text = try? Data(contentsOf: dotenv),
                  let decoded = String(data: text, encoding: .utf8) else {
                throw Failure.unreadable
            }
            original = decoded
            permissions = (try? fileManager.attributesOfItem(atPath: dotenv.path))?[.posixPermissions] as? NSNumber
        } else {
            original = nil
            permissions = nil
        }

        var lines: [String]
        if let original {
            lines = original.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
            // A trailing newline shows up as a final empty element; it is put
            // back below.
            if lines.last == "" { lines.removeLast() }
            var replaced = false
            for index in lines.indices where key(of: lines[index]) == "PWA_PORT" {
                lines[index] = line
                replaced = true
            }
            if !replaced { lines.append(line) }
        } else {
            lines = [line]
        }

        let data = Data((lines.joined(separator: "\n") + "\n").utf8)
        try data.write(to: dotenv, options: .atomic)
        try? fileManager.setAttributes(
            [.posixPermissions: permissions ?? NSNumber(value: 0o600)],
            ofItemAtPath: dotenv.path
        )
    }

    /// The key of one line, using the reader's rules (`LocalInstallation.dotenvValue`):
    /// comments are skipped, `export ` is stripped, and the key is what comes
    /// before the first `=`.
    private static func key(of rawLine: String) -> String? {
        var line = rawLine.trimmingCharacters(in: .whitespacesAndNewlines)
        if line.hasPrefix("export ") {
            line = String(line.dropFirst(7)).trimmingCharacters(in: .whitespacesAndNewlines)
        }
        guard !line.hasPrefix("#"), let equals = line.firstIndex(of: "=") else { return nil }
        return line[..<equals].trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

/// A port the kernel reported as free when it was picked by binding to it and
/// closing. If the port is taken before the engine binds it, the engine fails
/// the same way again and the same recovery is offered.
enum FreePort {
    static func pick() -> Int? {
        let descriptor = socket(AF_INET, SOCK_STREAM, 0)
        guard descriptor >= 0 else { return nil }
        defer { close(descriptor) }

        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = 0
        address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
        let bound = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(descriptor, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0 else { return nil }

        var assigned = sockaddr_in()
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        let named = withUnsafeMutablePointer(to: &assigned) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                getsockname(descriptor, $0, &length)
            }
        }
        guard named == 0 else { return nil }

        let port = Int(UInt16(bigEndian: assigned.sin_port))
        return (1...65535).contains(port) ? port : nil
    }
}
