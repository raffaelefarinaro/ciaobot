import Darwin
import Foundation

/// A small framed local transport: a Unix domain socket, one request and one
/// reply per connection, each a 4-byte big-endian length followed by that many
/// bytes of JSON. A dev and test transport: the production transport is a
/// system-domain Mach service (XPC), a later slice.
///
/// Hardening that applies here: the socket is created owner-only (0600) inside
/// a directory the caller controls; the peer's effective uid is checked with
/// `getpeereid` and a peer other than `allowedUID` gets silence; every
/// connection has a total deadline (no trickling); requests are capped; a
/// malformed or oversized frame is dropped without a reply.
public final class SocketServer {
    public static let callDeadline: TimeInterval = 2.0

    private let path: String
    private let allowedUID: uid_t
    private let handler: ([UInt8]) -> [UInt8]?
    private var listenFD: Int32 = -1
    private var wakeRead: Int32 = -1
    private var wakeWrite: Int32 = -1
    private var thread: Thread?
    private let done = DispatchSemaphore(value: 0)

    public init(path: String, allowedUID: uid_t, handler: @escaping ([UInt8]) -> [UInt8]?) {
        self.path = path
        self.allowedUID = allowedUID
        self.handler = handler
    }

    public func start() throws {
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(path.utf8)
        let capacity = MemoryLayout.size(ofValue: addr.sun_path)
        guard pathBytes.count < capacity else { throw StoreError("socket path too long") }
        // A leftover socket from a previous run is replaced; anything else is an error.
        var info = stat()
        if lstat(path, &info) == 0 {
            guard info.st_mode & S_IFMT == S_IFSOCK, info.st_uid == geteuid() else {
                throw StoreError("\(path) exists and is not our socket")
            }
            unlink(path)
        }
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw StoreError("socket: \(String(cString: strerror(errno)))") }
        _ = fcntl(fd, F_SETFD, FD_CLOEXEC)
        withUnsafeMutableBytes(of: &addr.sun_path) { raw in
            for (i, b) in pathBytes.enumerated() { raw[i] = b }
            raw[pathBytes.count] = 0
        }
        let old = umask(0o177)
        let bound = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        umask(old)
        guard bound == 0, listen(fd, 16) == 0 else {
            close(fd)
            throw StoreError("bind/listen: \(String(cString: strerror(errno)))")
        }
        var pipeFDs: [Int32] = [0, 0]
        guard pipe(&pipeFDs) == 0 else { close(fd); throw StoreError("pipe failed") }
        listenFD = fd
        wakeRead = pipeFDs[0]
        wakeWrite = pipeFDs[1]
        let t = Thread { [self] in
            serve()
            done.signal()
        }
        t.name = "ciao-authority-socket"
        thread = t
        t.start()
    }

    public func stop() {
        guard listenFD >= 0 else { return }
        var byte: UInt8 = 1
        _ = write(wakeWrite, &byte, 1)
        done.wait()
        close(listenFD)
        close(wakeRead)
        close(wakeWrite)
        unlink(path)
        listenFD = -1
    }

    private func serve() {
        while true {
            var fds = [pollfd(fd: listenFD, events: Int16(POLLIN), revents: 0), pollfd(fd: wakeRead, events: Int16(POLLIN), revents: 0)]
            if poll(&fds, 2, -1) < 0 { if errno == EINTR { continue }; return }
            if fds[1].revents != 0 { return }
            guard fds[0].revents & Int16(POLLIN) != 0 else { continue }
            let conn = accept(listenFD, nil, nil)
            if conn < 0 { continue }
            handle(conn)
            close(conn)
        }
    }

    private func handle(_ conn: Int32) {
        var one: Int32 = 1
        setsockopt(conn, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout<Int32>.size))
        var uid: uid_t = 0
        var gid: gid_t = 0
        guard getpeereid(conn, &uid, &gid) == 0, uid == allowedUID else { return }  // silence
        let deadline = Date().addingTimeInterval(Self.callDeadline)
        guard let header = Self.readExactly(conn, 4, deadline: deadline) else { return }
        let length = Int(header[0]) << 24 | Int(header[1]) << 16 | Int(header[2]) << 8 | Int(header[3])
        guard length > 0, length <= maxRequestBytes, let request = Self.readExactly(conn, length, deadline: deadline) else { return }
        guard let reply = handler(request), reply.count <= maxReplyBytes else { return }
        let n = reply.count
        let frame = [UInt8(n >> 24 & 0xff), UInt8(n >> 16 & 0xff), UInt8(n >> 8 & 0xff), UInt8(n & 0xff)] + reply
        Self.writeAll(conn, frame, deadline: deadline)
    }

    static func readExactly(_ fd: Int32, _ count: Int, deadline: Date) -> [UInt8]? {
        var out = [UInt8](repeating: 0, count: count)
        var done = 0
        while done < count {
            let remaining = deadline.timeIntervalSinceNow
            guard remaining > 0 else { return nil }
            var p = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
            let ready = poll(&p, 1, Int32(min(remaining * 1000, 2000)))
            if ready < 0 { if errno == EINTR { continue }; return nil }
            if ready == 0 { continue }
            let n = out.withUnsafeMutableBytes { read(fd, $0.baseAddress! + done, count - done) }
            if n <= 0 { if n < 0 && errno == EINTR { continue }; return nil }
            done += n
        }
        return out
    }

    static func writeAll(_ fd: Int32, _ data: [UInt8], deadline: Date) {
        var done = 0
        while done < data.count {
            let remaining = deadline.timeIntervalSinceNow
            guard remaining > 0 else { return }
            var p = pollfd(fd: fd, events: Int16(POLLOUT), revents: 0)
            let ready = poll(&p, 1, Int32(min(remaining * 1000, 2000)))
            if ready < 0 { if errno == EINTR { continue }; return }
            if ready == 0 { continue }
            let n = data.withUnsafeBytes { write(fd, $0.baseAddress! + done, data.count - done) }
            if n <= 0 { if n < 0 && errno == EINTR { continue }; return }
            done += n
        }
    }
}
