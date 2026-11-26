import Darwin
import XCTest

@testable import CiaobotAuthority

final class WireTests: XCTestCase {
    let op = OperatorTestSigner()
    let nonce = String(repeating: "ab", count: 16)

    /// An authority with one grant and two events (counter 3).
    private func host() throws -> (AuthorityHost, ChatAuthority) {
        let a = try makeAuthority(ids: ["g1", "t1", "e1", "t2", "e2"])
        _ = try a.createGrant(
            op.sign(
                CommandKind.grant, 1,
                [
                    "workspace": .string("w1"), "project": .string("p1"), "chat": .null, "destination": .string("brain"),
                    "audience": .string("team"), "producers": .array([.string("memory_extraction")]),
                ]))
        for (n, c) in ["c1", "c2"].enumerated() {
            _ = try a.submitTurn(
                op.sign(
                    CommandKind.submit, Int64(n + 2),
                    ["workspace": .string("w1"), "project": .string("p1"), "chat": .string(c), "send_id": .string("s\(n)")],
                    payload: Array("msg \(n) \u{e9}".utf8)))
        }
        return (AuthorityHost(.ready(a)), a)
    }

    private func call(_ host: AuthorityHost, _ fields: [String: JSONValue]) throws -> [String: JSONValue]? {
        var req: [String: JSONValue] = ["v": .int(1), "nonce": .string(nonce)]
        req.merge(fields) { _, n in n }
        guard let reply = host.handle(JSON.canonical(.object(req))) else { return nil }
        return try JSON.parse(reply).objectValue
    }

    private let lookup: [String: JSONValue] = [
        "op": .string("eligible_events"), "grant_id": .string("g1"), "producer": .string("memory_extraction"),
        "limit": .int(10), "destination": .string("brain"), "audience": .string("team"), "chat": .null,
        "expected_epoch": .null,
    ]

    func testHealthEchoesTheNonceAndReportsState() throws {
        let (h, a) = try host()
        let r = try XCTUnwrap(try call(h, ["op": .string("health")]))
        XCTAssertEqual(r["nonce"], .string(nonce))
        XCTAssertEqual(r["v"], .int(1))
        XCTAssertEqual(r["ok"], .bool(true))
        let result = try XCTUnwrap(r["result"]?.objectValue)
        XCTAssertEqual(result["ready"], .bool(true))
        XCTAssertEqual(result["state_counter"], .int(3))
        XCTAssertEqual(result["epoch"], .int(0))
        XCTAssertEqual(result["authority_key_id"], .string(a.authorityKeyID))
        XCTAssertEqual(result["event_public_key"], .string(Vectors.string(Vectors.keys, "event_public_hex")))
        XCTAssertEqual(result["recovered_crash_window"], .bool(false))
    }

    func testEligibleEventsReturnsNewestFirstWithEchoedScope() throws {
        let (h, _) = try host()
        let r = try XCTUnwrap(try call(h, lookup))
        let result = try XCTUnwrap(r["result"]?.objectValue)
        XCTAssertEqual(result["grant_id"], .string("g1"))
        XCTAssertEqual(result["destination"], .string("brain"))
        XCTAssertEqual(result["audience"], .string("team"))
        let events = try XCTUnwrap(result["events"]?.arrayValue)
        XCTAssertEqual(events.map { $0.objectValue?["send_id"]?.stringValue }, ["s1", "s0"])
        let first = try XCTUnwrap(events.first?.objectValue)
        for key in ["event_id", "turn_id", "revision", "seq", "send_id", "workspace", "project", "chat", "principal", "text_b64", "text_sha256", "opted_out", "grant_ids", "epoch", "signature_b64"] {
            XCTAssertNotNil(first[key], key)
        }
        // chat filter and limit apply before the cut.
        var narrowed = lookup
        narrowed["chat"] = .string("c1")
        narrowed["limit"] = .int(1)
        let one = try XCTUnwrap(try call(h, narrowed)?["result"]?.objectValue?["events"]?.arrayValue)
        XCTAssertEqual(one.map { $0.objectValue?["send_id"]?.stringValue }, ["s0"])
    }

    func testWrongDestinationYieldsNothingAndStaleEpochIsRefused() throws {
        let (h, _) = try host()
        var wrong = lookup
        wrong["destination"] = .string("elsewhere")
        let events = try XCTUnwrap(try call(h, wrong)?["result"]?.objectValue?["events"]?.arrayValue)
        XCTAssertEqual(events.count, 0)
        var stale = lookup
        stale["expected_epoch"] = .int(9)
        let r = try XCTUnwrap(try call(h, stale))
        XCTAssertEqual(r["ok"], .bool(false))
        XCTAssertEqual(r["code"], .string("epoch_changed"))
        XCTAssertEqual(r["nonce"], .string(nonce))
    }

    func testBadRequestsAndVersions() throws {
        let (h, _) = try host()
        var noLimit = lookup
        noLimit["limit"] = .int(0)
        XCTAssertEqual(try call(h, noLimit)?["code"], .string("bad_request"))
        var typo = lookup
        typo["producer"] = .int(3)
        XCTAssertEqual(try call(h, typo)?["code"], .string("bad_request"))
        XCTAssertEqual(try call(h, ["op": .string("create_grant")])?["code"], .string("unknown_op"))
        XCTAssertEqual(try call(h, ["op": .string("health"), "v": .int(2)])?["code"], .string("bad_version"))
    }

    func testMalformedRequestsGetNoReply() throws {
        let (h, _) = try host()
        XCTAssertNil(h.handle(Array("not json".utf8)))
        XCTAssertNil(h.handle(JSON.canonical(.object(["v": .int(1), "op": .string("health")]))))  // no nonce
        XCTAssertNil(h.handle(JSON.canonical(.object(["v": .int(1), "op": .string("health"), "nonce": .string("short")]))))
        XCTAssertNil(h.handle([UInt8](repeating: 0x20, count: maxRequestBytes + 1)))
    }

    func testClosedHostReportsNotReadyAndRefusesLookups() throws {
        let h = AuthorityHost(.closed(reason: "state_rollback"))
        let health = try XCTUnwrap(try call(h, ["op": .string("health")])?["result"]?.objectValue)
        XCTAssertEqual(health["ready"], .bool(false))
        XCTAssertEqual(health["code"], .string("state_rollback"))
        XCTAssertEqual(try call(h, lookup)?["code"], .string("closed"))
    }

    func testAChangedEventSignatureNeverLeavesTheDaemon() throws {
        // verify_event runs inside the lookup: a stored event that fails its signature is dropped.
        let storage = MemoryStorage(), anchor = MemoryAnchor()
        let a = try makeAuthority(ids: ["g1", "t1", "e1"], storage: storage, anchor: anchor)
        _ = try a.createGrant(op.sign(CommandKind.grant, 1, ["workspace": .string("w1"), "project": .string("p1"), "chat": .null, "destination": .string("brain"), "audience": .string("team"), "producers": .array([.string("memory_extraction")])]))
        let e = try a.submitTurn(op.sign(CommandKind.submit, 2, ["workspace": .string("w1"), "project": .string("p1"), "chat": .string("c1"), "send_id": .string("s")], payload: Array("x".utf8)))
        XCTAssertTrue(a.verifyEvent(e))
        var forged = e
        forged.text = Array("y".utf8)
        XCTAssertFalse(a.verifyEvent(forged))
        forged = e
        forged.signature[0] ^= 1
        XCTAssertFalse(a.verifyEvent(forged))
    }

    // MARK: socket

    private func exchange(_ path: String, _ payload: [UInt8], timeout: TimeInterval = 3) -> [UInt8]? {
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        defer { close(fd) }
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        withUnsafeMutableBytes(of: &addr.sun_path) { raw in
            for (i, b) in path.utf8.enumerated() { raw[i] = b }
        }
        let ok = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        guard ok == 0 else { return nil }
        let n = payload.count
        let frame = [UInt8(n >> 24 & 0xff), UInt8(n >> 16 & 0xff), UInt8(n >> 8 & 0xff), UInt8(n & 0xff)] + payload
        let deadline = Date().addingTimeInterval(timeout)
        SocketServer.writeAll(fd, frame, deadline: deadline)
        guard let header = SocketServer.readExactly(fd, 4, deadline: deadline) else { return nil }
        let length = Int(header[0]) << 24 | Int(header[1]) << 16 | Int(header[2]) << 8 | Int(header[3])
        return SocketServer.readExactly(fd, length, deadline: deadline)
    }

    func testSocketRoundTripAndLifecycle() throws {
        let dir = try temporaryDirectory("sock")
        defer { try? FileManager.default.removeItem(atPath: dir) }
        let (h, _) = try host()
        let path = dir + "/s"
        let server = SocketServer(path: path, allowedUID: geteuid(), handler: h.handle)
        try server.start()
        var info = stat()
        XCTAssertEqual(lstat(path, &info), 0)
        XCTAssertEqual(info.st_mode & 0o777, 0o600)
        let reply = try XCTUnwrap(exchange(path, JSON.canonical(.object(["v": .int(1), "op": .string("health"), "nonce": .string(nonce)]))))
        XCTAssertEqual(try JSON.parse(reply).objectValue?["nonce"], .string(nonce))
        // Several sequential calls on separate connections.
        for _ in 0..<5 { XCTAssertNotNil(exchange(path, JSON.canonical(.object(["v": .int(1), "op": .string("health"), "nonce": .string(nonce)])))) }
        // A malformed request gets silence, and the server keeps serving.
        XCTAssertNil(exchange(path, Array("garbage".utf8)))
        XCTAssertNotNil(exchange(path, JSON.canonical(.object(["v": .int(1), "op": .string("health"), "nonce": .string(nonce)]))))
        server.stop()
        XCTAssertNotEqual(lstat(path, &info), 0, "stop removes the socket")
    }

    func testSocketPeerWithAnotherUIDGetsSilence() throws {
        let dir = try temporaryDirectory("sock")
        defer { try? FileManager.default.removeItem(atPath: dir) }
        let (h, _) = try host()
        let path = dir + "/s"
        let server = SocketServer(path: path, allowedUID: geteuid() &+ 1, handler: h.handle)
        try server.start()
        defer { server.stop() }
        XCTAssertNil(exchange(path, JSON.canonical(.object(["v": .int(1), "op": .string("health"), "nonce": .string(nonce)])), timeout: 2.5))
    }

    func testOversizedFrameIsDroppedAndASlowClientDoesNotWedgeTheServer() throws {
        let dir = try temporaryDirectory("sock")
        defer { try? FileManager.default.removeItem(atPath: dir) }
        let (h, _) = try host()
        let path = dir + "/s"
        let server = SocketServer(path: path, allowedUID: geteuid(), handler: h.handle)
        try server.start()
        defer { server.stop() }
        // Declares 16 MiB: dropped before any body is read.
        let big = [0x01, 0x00, 0x00, 0x00] as [UInt8]
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        defer { close(fd) }
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        withUnsafeMutableBytes(of: &addr.sun_path) { raw in for (i, b) in path.utf8.enumerated() { raw[i] = b } }
        _ = withUnsafePointer(to: &addr) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) } }
        SocketServer.writeAll(fd, big, deadline: Date().addingTimeInterval(1))
        XCTAssertNil(SocketServer.readExactly(fd, 4, deadline: Date().addingTimeInterval(1)))
        // A client that connects and says nothing is dropped at the deadline; then a real call works.
        let idle = socket(AF_UNIX, SOCK_STREAM, 0)
        defer { close(idle) }
        _ = withUnsafePointer(to: &addr) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(idle, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) } }
        let started = Date()
        XCTAssertNotNil(exchange(path, JSON.canonical(.object(["v": .int(1), "op": .string("health"), "nonce": .string(nonce)])), timeout: 5))
        XCTAssertLessThan(Date().timeIntervalSince(started), SocketServer.callDeadline + 1.5)
    }

    func testStartRefusesToReplaceAnUnrelatedFile() throws {
        let dir = try temporaryDirectory("sock")
        defer { try? FileManager.default.removeItem(atPath: dir) }
        let path = dir + "/s"
        FileManager.default.createFile(atPath: path, contents: Data("x".utf8))
        let (h, _) = try host()
        XCTAssertThrowsError(try SocketServer(path: path, allowedUID: geteuid(), handler: h.handle).start())
        XCTAssertEqual(try String(contentsOfFile: path, encoding: .utf8), "x")
    }
}
