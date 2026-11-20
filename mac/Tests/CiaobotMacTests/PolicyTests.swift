import XCTest
@testable import CiaobotMac

final class PolicyTests: XCTestCase {
    func testOrigins() {
        for value in ["http://localhost:8443", "http://127.0.0.1", "http://[::1]:8443", "https://192.168.1.2", "https://Example.COM", "https://example.com/"] { XCTAssertNotNil(Origin(value), value) }
        for value in ["http://192.168.1.2", "http://localhost.evil", "http://127.1", "http://2130706433", "http://0177.0.0.1", "https://user:pass@example.com", "https://example.com/a", "https://example.com?x=1", "https://example.com#x", "https://example.com:", "https://example.com:0", "garbage"] { XCTAssertNil(Origin(value), value) }
    }
    /// The empty path and an ordinary root slash are the same origin. Every
    /// other path spelling stays rejected, and credentials/paths/queries are
    /// still never accepted.
    func testRootSlashIsNormalisedToTheBareOrigin() {
        let bare = Origin("https://example.com")!
        let slash = Origin("https://example.com/")!
        XCTAssertEqual(slash.canonicalKey, bare.canonicalKey)
        XCTAssertEqual(slash.url.absoluteString, "https://example.com")
        XCTAssertEqual(slash.navigation(URL(string: "https://example.com/chat")!, userInitiated: false), .inWindow)
        XCTAssertEqual(bare.navigation(URL(string: "https://example.com/")!, userInitiated: false), .inWindow)
        XCTAssertEqual(
            Origin("http://127.0.0.1:8443/")!.canonicalKey,
            Origin("http://127.0.0.1:8443")!.canonicalKey
        )
        XCTAssertEqual(Origin("https://192.168.1.2/")!.url.absoluteString, "https://192.168.1.2")
        for value in [
            "https://example.com//",
            "https://example.com/./",
            "https://example.com/..",
            "https://example.com/.",
            "https://example.com/%2F",
            "https://example.com/%2f/",
            "https://example.com/chat/",
            "https://example.com/?",
            "https://example.com/?x=1",
            "https://example.com/#",
            "https://example.com/#f",
            "https://user:pass@example.com/",
            "https://example.com:0/",
            "https://example.com./",
        ] {
            XCTAssertNil(Origin(value), value)
        }
    }
    func testAmbiguousOriginsAreRejected() {
        for value in [
            "https://example.com%40evil.com",
            "https://example.com%2f@evil.com",
            "https://example.com\\@evil.com",
            "https://example.com%00.evil.com",
            "https://example.com%2e.evil.com",
            "https://example.com./",
            "https://2130706433",
            "https://127.1",
        ] { XCTAssertNil(Origin(value), value) }
    }
    func testLoopbackClassification() {
        XCTAssertTrue(Origin("http://localhost:8443")!.isLoopback)
        XCTAssertTrue(Origin("http://127.0.0.1:8443")!.isLoopback)
        XCTAssertTrue(Origin("http://[::1]:8443")!.isLoopback)
        XCTAssertFalse(Origin("https://example.com")!.isLoopback)
        XCTAssertFalse(Origin("https://192.168.1.2")!.isLoopback)
    }
    func testNavigation() {
        let origin = Origin("https://Example.com")!
        XCTAssertEqual(origin.navigation(URL(string: "https://example.COM:443/chat")!, userInitiated: false), .inWindow)
        XCTAssertEqual(origin.navigation(URL(string: "https://example.com:444/chat")!, userInitiated: true), .external)
        XCTAssertEqual(origin.navigation(URL(string: "http://example.com/")!, userInitiated: true), .external)
        XCTAssertEqual(origin.navigation(URL(string: "http://example.com/")!, userInitiated: false), .blocked)
        XCTAssertEqual(origin.navigation(URL(string: "http://preview.example.com:8767/talk")!, userInitiated: true), .external)
        XCTAssertEqual(origin.navigation(URL(string: "https://elsewhere.com")!, userInitiated: false), .blocked)
        XCTAssertEqual(origin.navigation(URL(string: "mailto:a@example.com")!, userInitiated: true), .external)
        XCTAssertEqual(origin.navigation(URL(string: "file:///etc/passwd")!, userInitiated: true), .blocked)
        XCTAssertEqual(origin.navigation(URL(string: "https://u:p@example.com")!, userInitiated: true), .blocked)
        XCTAssertNotEqual(Origin("http://localhost:8443")!.navigation(URL(string: "http://127.0.0.1:8443")!, userInitiated: false), .inWindow)
    }
    func testDefaultPortsAreEffectivePorts() {
        let http = Origin("http://127.0.0.1")!
        XCTAssertEqual(http.navigation(URL(string: "http://127.0.0.1:80/chat")!, userInitiated: false), .inWindow)
        XCTAssertEqual(http.navigation(URL(string: "https://127.0.0.1/chat")!, userInitiated: false), .blocked)

        let https = Origin("https://example.com")!
        XCTAssertEqual(https.navigation(URL(string: "https://example.com:443/chat")!, userInitiated: false), .inWindow)
        XCTAssertEqual(https.navigation(URL(string: "https://example.com:0443/chat")!, userInitiated: false), .inWindow)
        XCTAssertEqual(https.navigation(URL(string: "https://example.com:444/chat")!, userInitiated: false), .blocked)
    }
    func testUnsafeNavigationTargetsAreRejected() {
        let origin = Origin("https://example.com")!
        for value in [
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "blob:https://example.com/0000",
            "file:///etc/passwd",
            "https://user:pass@example.com/",
            "https://example.com%2f@evil.com/",
            "https://example.com%40evil.com/",
            "https://example.com./",
        ] {
            XCTAssertNotEqual(origin.navigation(URL(string: value)!, userInitiated: true), .inWindow, value)
        }
        XCTAssertEqual(origin.navigation(URL(string: "https://example.com./")!, userInitiated: true), .external)
    }
    func testProbeResponse() {
        XCTAssertTrue(Probe.valid(status: 200, data: Data(#"{"startup_id":"abc"}"#.utf8)))
        for body in [#"{"startup_id":""}"#, #"{"startup_id":"  "}"#, #"{"startup_id":1}"#, "[]", "oops"] { XCTAssertFalse(Probe.valid(status: 200, data: Data(body.utf8))) }
        XCTAssertFalse(Probe.valid(status: 302, data: Data(#"{"startup_id":"abc"}"#.utf8)))
        XCTAssertFalse(Probe.valid(status: 401, data: Data(#"{"startup_id":"abc"}"#.utf8)))
    }
}

final class HostValidationTests: XCTestCase {
    func testValidHosts() {
        for host in ["example.com", "a-b.example", "localhost", "127.0.0.1", "[::1]", "[2001:db8::1]"] {
            XCTAssertNotNil(Origin("https://\(host)"), host)
        }
    }
    func testInvalidHosts() {
        for host in ["-", ".", "example.com..", "-foo.example", "foo-.example", "foo..example", "a_b.example", "999.1.1.1", "[:::1]"] {
            XCTAssertNil(Origin("https://\(host)"), host)
        }
    }
}
