import Darwin
import Foundation
import XCTest

@testable import CiaobotMac

/// The `.env` writer touches one line of a file that usually holds secrets.
/// Everything else in it must survive byte-for-byte.
final class WorkspacePortTests: XCTestCase {
    private var root: URL!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory
            .appendingPathComponent("workspace-port-tests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: root.path)
        try? FileManager.default.removeItem(at: root)
    }

    private var dotenv: URL { root.appendingPathComponent(".env") }

    private func contents() throws -> String {
        try String(contentsOf: dotenv, encoding: .utf8)
    }

    func testWritingThePortPreservesEveryOtherLine() throws {
        try Data("SECRET_KEY=hunter2\nPWA_PORT=8443\n# keep me\n".utf8).write(to: dotenv)

        try WorkspaceDotenvPort.setPort(8555, in: dotenv)

        XCTAssertEqual(try contents(), "SECRET_KEY=hunter2\nPWA_PORT=8555\n# keep me\n")
    }

    func testAnExportedOrQuotedPortLineIsRewrittenInPlace() throws {
        try Data("# above\n\nexport PWA_PORT=\"9555\"\nAFTER=1\n".utf8).write(to: dotenv)

        try WorkspaceDotenvPort.setPort(8555, in: dotenv)

        XCTAssertEqual(try contents(), "# above\n\nPWA_PORT=8555\nAFTER=1\n")
    }

    func testAMissingDotenvIsCreatedWithOnlyThePort() throws {
        try WorkspaceDotenvPort.setPort(8555, in: dotenv)

        XCTAssertEqual(try contents(), "PWA_PORT=8555\n")
        let attributes = try FileManager.default.attributesOfItem(atPath: dotenv.path)
        XCTAssertEqual((attributes[.posixPermissions] as? NSNumber)?.intValue, 0o600)
    }

    func testAnUnwritablePathThrowsAndCreatesNothing() throws {
        try FileManager.default.setAttributes([.posixPermissions: 0o500], ofItemAtPath: root.path)

        XCTAssertThrowsError(try WorkspaceDotenvPort.setPort(8555, in: dotenv))
        XCTAssertFalse(FileManager.default.fileExists(atPath: dotenv.path))
    }

    func testPickReturnsAnImmediatelyBindablePort() throws {
        let port = try XCTUnwrap(FreePort.pick())
        XCTAssertTrue((1...65535).contains(port))

        let fd = socket(AF_INET, SOCK_STREAM, 0)
        XCTAssertGreaterThanOrEqual(fd, 0)
        defer { close(fd) }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = UInt16(port).bigEndian
        address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
        let bound = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        XCTAssertEqual(bound, 0, "the port picked must still be bindable")
    }
}
