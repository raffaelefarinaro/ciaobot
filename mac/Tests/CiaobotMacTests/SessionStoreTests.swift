import XCTest
import Foundation
import Darwin

@testable import CiaobotMac

/// A test double for the WebKit eraser. It records the identifiers it was
/// asked to remove; store-identifier and cookie values are never logged.
@MainActor
private final class RecordingEraser: WebsiteDataStoreErasing {
    private(set) var removed: [UUID] = []
    var failure: Error?

    func remove(identifier: UUID, completion: @escaping (Error?) -> Void) {
        removed.append(identifier)
        completion(failure)
    }
}

private struct FixtureError: Error, Equatable {}

/// Fails `createDirectory` a fixed number of times so a mapping write can fail
/// transiently without deleting or replacing the file on disk.
private final class FailingFileManager: FileManager {
    var failuresRemaining = 0

    override func createDirectory(at url: URL, withIntermediateDirectories createIntermediates: Bool, attributes: [FileAttributeKey: Any]? = nil) throws {
        if failuresRemaining > 0 {
            failuresRemaining -= 1
            throw CocoaError(.fileWriteNoPermission)
        }
        try super.createDirectory(at: url, withIntermediateDirectories: createIntermediates, attributes: attributes)
    }
}

func assertForgetSucceeded(_ result: Result<Void, ForgetFailure>?, file: StaticString = #filePath, line: UInt = #line) {
    guard let result else { return XCTFail("forget did not complete", file: file, line: line) }
    if case .failure(let error) = result {
        XCTFail("expected success, got \(error)", file: file, line: line)
    }
}

func assertForgetFailed(_ result: Result<Void, ForgetFailure>?, with expected: ForgetFailure, file: StaticString = #filePath, line: UInt = #line) {
    guard let result else { return XCTFail("forget did not complete", file: file, line: line) }
    switch result {
    case .success:
        XCTFail("expected \(expected), got success", file: file, line: line)
    case .failure(let error):
        XCTAssertEqual(error, expected, file: file, line: line)
    }
}

func temporaryDirectory() throws -> URL {
    let directory = FileManager.default.temporaryDirectory
        .appendingPathComponent("ciaobot-session-tests-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    return directory
}

func makeRegistry(in directory: URL? = nil, name: String = "session-stores.json") throws -> (SessionStoreRegistry, URL) {
    let directory = try directory ?? temporaryDirectory()
    let file = directory.appendingPathComponent(name)
    return (SessionStoreRegistry(fileURL: file), file)
}

final class SessionOriginKeyTests: XCTestCase {
    func testCanonicalKeyIncludesTheEffectivePort() {
        XCTAssertEqual(Origin("https://example.com")!.canonicalKey, "https://example.com:443")
        XCTAssertEqual(Origin("https://example.com:443")!.canonicalKey, "https://example.com:443")
        XCTAssertEqual(Origin("https://EXAMPLE.com:444")!.canonicalKey, "https://example.com:444")
        XCTAssertEqual(Origin("http://127.0.0.1:8443")!.canonicalKey, "http://127.0.0.1:8443")
        XCTAssertEqual(Origin("https://[::1]")!.canonicalKey, "https://[::1]:443")
    }

    func testOnlyHTTPSOriginsUseTLS() {
        XCTAssertTrue(Origin("https://example.com")!.usesTLS)
        XCTAssertFalse(Origin("http://127.0.0.1:8443")!.usesTLS)
        XCTAssertFalse(Origin("http://localhost")!.usesTLS)
    }
}

@MainActor
final class SessionStoragePolicyTests: XCTestCase {
    func testPersistentSignInIsOffByDefault() throws {
        let (registry, file) = try makeRegistry()
        let signIn = SessionSignIn(registry: registry, eraser: RecordingEraser(), persistentAvailable: true)
        let origin = Origin("https://example.com")!

        XCTAssertEqual(signIn.storage(for: origin, keepSignedIn: false), .ephemeral)
        XCTAssertFalse(signIn.hasSavedSignIn(for: origin))
        XCTAssertEqual(registry.mappedOriginCount, 0)
        XCTAssertFalse(FileManager.default.fileExists(atPath: file.path),
                       "A connection that did not opt in must not create a mapping file")
    }

    func testHTTPOriginNeverGetsAPersistentStore() throws {
        let (registry, _) = try makeRegistry()
        let signIn = SessionSignIn(registry: registry, eraser: RecordingEraser(), persistentAvailable: true)
        let origin = Origin("http://127.0.0.1:8443")!

        XCTAssertEqual(signIn.storage(for: origin, keepSignedIn: true), .ephemeral)
        XCTAssertEqual(signIn.fallback(for: origin, keepSignedIn: true), .insecureOrigin)
        XCTAssertFalse(signIn.hasSavedSignIn(for: origin))
        XCTAssertEqual(registry.mappedOriginCount, 0)
    }

    func testUnsupportedSystemFallsBackToEphemeral() throws {
        let (registry, _) = try makeRegistry()
        let signIn = SessionSignIn(registry: registry, eraser: RecordingEraser(), persistentAvailable: false)
        let origin = Origin("https://example.com")!

        XCTAssertEqual(signIn.storage(for: origin, keepSignedIn: true), .ephemeral)
        XCTAssertEqual(signIn.fallback(for: origin, keepSignedIn: true), .unsupportedSystem)
        XCTAssertEqual(registry.mappedOriginCount, 0)
    }

    func testSavedSignInIsNotUsedWithoutTheOptIn() throws {
        let (registry, _) = try makeRegistry()
        let origin = Origin("https://example.com")!
        _ = try XCTUnwrap(registry.identifier(for: origin))
        let signIn = SessionSignIn(registry: registry, eraser: RecordingEraser(), persistentAvailable: true)

        XCTAssertTrue(signIn.hasSavedSignIn(for: origin))
        XCTAssertEqual(signIn.storage(for: origin, keepSignedIn: false), .ephemeral,
                       "A mapping on disk must never become an implicit opt-in")
    }

    func testNoFallbackForAHealthyOptInOrAnOptOut() throws {
        let (registry, _) = try makeRegistry()
        let origin = Origin("https://example.com")!
        let signIn = SessionSignIn(registry: registry, eraser: RecordingEraser(), persistentAvailable: true)

        XCTAssertNil(signIn.fallback(for: origin, keepSignedIn: true))
        XCTAssertNil(signIn.fallback(for: origin, keepSignedIn: false))
        guard case .persistent = signIn.storage(for: origin, keepSignedIn: true) else {
            return XCTFail("A healthy opt-in did not get a persistent store")
        }
    }

    func testOptedInHTTPSOriginGetsAStableIdentifierAcrossLaunches() throws {
        let directory = try temporaryDirectory()
        let (registry, file) = try makeRegistry(in: directory)
        let origin = Origin("https://example.com")!
        let first = try XCTUnwrap(registry.identifier(for: origin))

        let relaunched = SessionStoreRegistry(fileURL: file)
        XCTAssertTrue(relaunched.isAvailable)
        XCTAssertEqual(relaunched.identifierIfPresent(for: origin), first)
        XCTAssertEqual(relaunched.identifier(for: origin), first)

        let signIn = SessionSignIn(registry: relaunched, eraser: RecordingEraser(), persistentAvailable: true)
        XCTAssertEqual(signIn.storage(for: origin, keepSignedIn: true), .persistent(first))
        XCTAssertTrue(signIn.hasSavedSignIn(for: origin))
    }

    func testDistinctPortsGetDistinctStoreIdentifiers() throws {
        let (registry, _) = try makeRegistry()
        let first = try XCTUnwrap(registry.identifier(for: Origin("https://example.com")!))
        let second = try XCTUnwrap(registry.identifier(for: Origin("https://example.com:444")!))
        XCTAssertNotEqual(first, second, "Cookies ignore ports, so each port needs its own store")
    }

    func testDistinctLoopbackPortsGetDistinctStoreIdentifiers() throws {
        let (registry, _) = try makeRegistry()
        let first = try XCTUnwrap(registry.identifier(for: Origin("https://127.0.0.1:8443")!))
        let second = try XCTUnwrap(registry.identifier(for: Origin("https://127.0.0.1:8444")!))
        XCTAssertNotEqual(first, second)
    }

    func testDefaultPortFormsShareOneCanonicalStore() throws {
        let (registry, _) = try makeRegistry()
        let bare = try XCTUnwrap(registry.identifier(for: Origin("https://example.com")!))
        XCTAssertEqual(registry.identifier(for: Origin("https://example.com:443")!), bare)
        XCTAssertEqual(registry.identifier(for: Origin("https://EXAMPLE.com:443")!), bare)
        XCTAssertEqual(registry.mappedOriginCount, 1)
    }

    func testRegistryPersistsOnlyOriginsAndIdentifiersPrivately() throws {
        let (registry, file) = try makeRegistry()
        _ = try XCTUnwrap(registry.identifier(for: Origin("https://example.com")!))
        _ = try XCTUnwrap(registry.identifier(for: Origin("https://example.com:444")!))

        let data = try Data(contentsOf: file)
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(Set(root.keys), ["version", "stores"])
        let stores = try XCTUnwrap(root["stores"] as? [[String: Any]])
        XCTAssertEqual(stores.count, 2)
        for store in stores {
            XCTAssertEqual(Set(store.keys), ["origin", "identifier"])
        }

        let text = String(decoding: data, as: UTF8.self).lowercased()
        XCTAssertFalse(text.contains("cookie"))
        XCTAssertFalse(text.contains("token"))
        XCTAssertFalse(text.contains("secret"))

        let fileMode = try XCTUnwrap(FileManager.default.attributesOfItem(atPath: file.path)[.posixPermissions] as? NSNumber)
        XCTAssertEqual(fileMode.intValue & 0o777, 0o600)
        let directoryMode = try XCTUnwrap(FileManager.default.attributesOfItem(atPath: file.deletingLastPathComponent().path)[.posixPermissions] as? NSNumber)
        XCTAssertEqual(directoryMode.intValue & 0o777, 0o700)

        // The advisory cross-process mutex lives beside the mapping and is
        // private too; it is synchronization, never authority.
        let lock = file.deletingLastPathComponent().appendingPathComponent("session-stores.lock")
        let lockMode = try XCTUnwrap(FileManager.default.attributesOfItem(atPath: lock.path)[.posixPermissions] as? NSNumber)
        XCTAssertEqual(lockMode.intValue & 0o777, 0o600)
    }

    func testCorruptRegistryFailsClosedToEphemeralStores() throws {
        let directory = try temporaryDirectory()
        let file = directory.appendingPathComponent("session-stores.json")
        try Data("not json".utf8).write(to: file)
        let registry = SessionStoreRegistry(fileURL: file)

        let signIn = SessionSignIn(registry: registry, eraser: RecordingEraser(), persistentAvailable: true)
        let origin = Origin("https://example.com")!

        XCTAssertFalse(registry.isAvailable)
        XCTAssertEqual(signIn.storage(for: origin, keepSignedIn: true), .ephemeral)
        XCTAssertEqual(signIn.fallback(for: origin, keepSignedIn: true), .registryUnavailable)
        XCTAssertFalse(signIn.hasSavedSignIn(for: origin))
    }

    func testDuplicateIdentifiersInRegistryFailClosed() throws {
        let directory = try temporaryDirectory()
        let file = directory.appendingPathComponent("session-stores.json")
        let identifier = UUID()
        let json = """
        {"version":1,"stores":[
          {"origin":"https://example.com:443","identifier":"\(identifier.uuidString)"},
          {"origin":"https://example.com:444","identifier":"\(identifier.uuidString)"}
        ]}
        """
        try Data(json.utf8).write(to: file)
        let registry = SessionStoreRegistry(fileURL: file)

        XCTAssertFalse(registry.isAvailable, "Two origins must never share a cookie store")
        XCTAssertNil(registry.identifier(for: Origin("https://example.com")!))
    }

    func testZeroIdentifierFailsClosed() throws {
        let directory = try temporaryDirectory()
        let file = directory.appendingPathComponent("session-stores.json")
        let json = """
        {"version":1,"stores":[{"origin":"https://example.com:443","identifier":"00000000-0000-0000-0000-000000000000"}]}
        """
        try Data(json.utf8).write(to: file)
        let registry = SessionStoreRegistry(fileURL: file)

        XCTAssertFalse(registry.isAvailable)
        XCTAssertNil(registry.identifier(for: Origin("https://example.com")!))
    }

    func testRegistryFallsBackWhenItsFileCannotBeCreated() throws {
        let registry = SessionStoreRegistry(fileURL: URL(fileURLWithPath: "/dev/null/missing/session-stores.json"))
        let signIn = SessionSignIn(registry: registry, eraser: RecordingEraser(), persistentAvailable: true)
        let origin = Origin("https://example.com")!

        XCTAssertEqual(signIn.storage(for: origin, keepSignedIn: true), .ephemeral)
        XCTAssertEqual(signIn.fallback(for: origin, keepSignedIn: true), .registryUnavailable)
    }

    func testNonCanonicalRegistryEntryFailsClosed() throws {
        let directory = try temporaryDirectory()
        let file = directory.appendingPathComponent("session-stores.json")
        // The app only ever writes canonical keys. Anything else means the
        // file was edited or written by something else, so no store is opened.
        for entry in ["https://example.com", "https://EXAMPLE.com:443", "https://example.com:443/"] {
            let json = """
            {"version":1,"stores":[{"origin":"\(entry)","identifier":"\(UUID().uuidString)"}]}
            """
            try Data(json.utf8).write(to: file)
            let registry = SessionStoreRegistry(fileURL: file)
            XCTAssertFalse(registry.isAvailable, "Accepted a non-canonical origin: \(entry)")
            XCTAssertNil(registry.identifier(for: Origin("https://example.com")!))
        }
    }

    func testInsecureRegistryEntryFailsClosed() throws {
        let directory = try temporaryDirectory()
        let file = directory.appendingPathComponent("session-stores.json")
        let json = """
        {"version":1,"stores":[{"origin":"http://127.0.0.1:8443","identifier":"\(UUID().uuidString)"}]}
        """
        try Data(json.utf8).write(to: file)
        let registry = SessionStoreRegistry(fileURL: file)

        XCTAssertFalse(registry.isAvailable, "An HTTP origin must never be mapped to a persistent store")
        XCTAssertEqual(registry.mappedOriginCount, 0)
    }

    func testCanonicalIPv6RegistryEntryIsAccepted() throws {
        let directory = try temporaryDirectory()
        let file = directory.appendingPathComponent("session-stores.json")
        let identifier = UUID()
        let json = """
        {"version":1,"stores":[{"origin":"https://[::1]:443","identifier":"\(identifier.uuidString)"}]}
        """
        try Data(json.utf8).write(to: file)
        let registry = SessionStoreRegistry(fileURL: file)

        XCTAssertTrue(registry.isAvailable)
        XCTAssertEqual(registry.identifierIfPresent(for: Origin("https://[::1]")!), identifier)
    }

    func testCachedIdentifierFailsClosedAfterAnotherInstanceRemovesTheMapping() throws {
        let directory = try temporaryDirectory()
        let (first, file) = try makeRegistry(in: directory)
        let origin = Origin("https://example.com")!
        let identifier = try XCTUnwrap(first.identifier(for: origin))

        // Another instance removes the mapping (and, in the app, its store).
        let second = SessionStoreRegistry(fileURL: file)
        XCTAssertEqual(second.identifierIfPresent(for: origin), identifier)
        try second.remove(origin)

        // The stale instance must not return its cached identifier; doing so
        // would reopen a store another instance already removed.
        XCTAssertNil(first.identifier(for: origin),
                     "A stale cache reopened an identifier that was removed on disk")
        XCTAssertFalse(first.isAvailable)
        XCTAssertEqual(first.storageUnavailableReason, .concurrentModification)
    }

    func testCachedIdentifierFailsClosedAfterAnotherInstanceRecreatesTheMapping() throws {
        let directory = try temporaryDirectory()
        let (first, file) = try makeRegistry(in: directory)
        let origin = Origin("https://example.com")!
        let original = try XCTUnwrap(first.identifier(for: origin))

        let second = SessionStoreRegistry(fileURL: file)
        try second.remove(origin)
        let recreated = try XCTUnwrap(second.identifier(for: origin))
        XCTAssertNotEqual(recreated, original, "Re-creating must allocate a new store")

        XCTAssertNil(first.identifier(for: origin),
                     "A stale cache returned an id replaced by another instance")
        XCTAssertFalse(first.isAvailable)
        XCTAssertEqual(first.storageUnavailableReason, .concurrentModification)
    }

    func testMappingWriteFailsFastWhenAnotherHolderHoldsTheLock() throws {
        let directory = try temporaryDirectory()
        let (registry, file) = try makeRegistry(in: directory)
        _ = try XCTUnwrap(registry.identifier(for: Origin("https://first.example")!))

        let lockURL = file.deletingLastPathComponent().appendingPathComponent("session-stores.lock")
        let holder = open(lockURL.path, O_RDWR)
        XCTAssertGreaterThanOrEqual(holder, 0)
        defer { close(holder) }
        XCTAssertEqual(flock(holder, LOCK_EX | LOCK_NB), 0)

        let started = Date()
        XCTAssertNil(registry.identifier(for: Origin("https://second.example")!),
                     "A held lock must fail closed")
        XCTAssertLessThan(Date().timeIntervalSince(started), 1.0,
                          "The main actor must never wait on the registry lock")
        XCTAssertEqual(registry.storageUnavailableReason, .concurrentModification)
        flock(holder, LOCK_UN)
    }

    func testStaleSnapshotFailsClosedInsteadOfLosingAnotherProcessMapping() throws {
        let directory = try temporaryDirectory()
        let (first, file) = try makeRegistry(in: directory)
        let firstOrigin = Origin("https://first.example")!
        let secondOrigin = Origin("https://second.example")!
        let thirdOrigin = Origin("https://third.example")!
        let firstIdentifier = try XCTUnwrap(first.identifier(for: firstOrigin))

        // A second process loads the same file, adds its own origin, and
        // writes the merged snapshot.
        let second = SessionStoreRegistry(fileURL: file)
        let secondIdentifier = try XCTUnwrap(second.identifier(for: secondOrigin))
        XCTAssertNotEqual(firstIdentifier, secondIdentifier)

        // The first process still holds its stale snapshot. Writing it would
        // drop the second origin's mapping, so it must fail closed instead.
        XCTAssertNil(first.identifier(for: thirdOrigin),
                     "A stale snapshot overwrote another process's mapping")
        XCTAssertFalse(first.isAvailable)
        XCTAssertEqual(first.storageUnavailableReason, .concurrentModification)

        // The file keeps both real mappings and no third one.
        let after = SessionStoreRegistry(fileURL: file)
        XCTAssertTrue(after.isAvailable)
        XCTAssertEqual(after.identifierIfPresent(for: firstOrigin), firstIdentifier)
        XCTAssertEqual(after.identifierIfPresent(for: secondOrigin), secondIdentifier)
        XCTAssertFalse(after.contains(thirdOrigin))
    }

    func testRegistryFailsClosedWhenAnotherProcessCreatesTheFileFirst() throws {
        let directory = try temporaryDirectory()
        let (first, file) = try makeRegistry(in: directory)
        let other = SessionStoreRegistry(fileURL: file)
        _ = try XCTUnwrap(other.identifier(for: Origin("https://second.example")!))

        XCTAssertNil(first.identifier(for: Origin("https://first.example")!))
        XCTAssertFalse(first.isAvailable)
        XCTAssertEqual(first.storageUnavailableReason, .concurrentModification)
    }

    func testCanPersistSignInRequiresASystemAndAUsableRegistry() throws {
        let (healthy, _) = try makeRegistry()
        XCTAssertTrue(SessionSignIn(registry: healthy, eraser: RecordingEraser(), persistentAvailable: true).canPersistSignIn)
        XCTAssertFalse(SessionSignIn(registry: healthy, eraser: RecordingEraser(), persistentAvailable: false).canPersistSignIn)

        let directory = try temporaryDirectory()
        let file = directory.appendingPathComponent("session-stores.json")
        try Data("not json".utf8).write(to: file)
        let corrupt = SessionStoreRegistry(fileURL: file)
        XCTAssertFalse(SessionSignIn(registry: corrupt, eraser: RecordingEraser(), persistentAvailable: true).canPersistSignIn)
    }
    func testConcurrentRegistryChangeUsesItsOwnFallback() throws {
        let directory = try temporaryDirectory()
        let (first, file) = try makeRegistry(in: directory)
        let origin = Origin("https://first.example")!
        let second = SessionStoreRegistry(fileURL: file)
        _ = try XCTUnwrap(second.identifier(for: Origin("https://second.example")!))

        let signIn = SessionSignIn(registry: first, eraser: RecordingEraser(), persistentAvailable: true)
        XCTAssertEqual(signIn.storage(for: origin, keepSignedIn: true), .ephemeral)
        XCTAssertEqual(signIn.fallback(for: origin, keepSignedIn: true), .registryChangedElsewhere)
    }
}

final class SessionStorageFallbackMessageTests: XCTestCase {
    func testRemovalVerificationSucceedsOnlyWhenTheStoreIsGone() {
        let identifier = UUID()
        let other = UUID()

        XCTAssertEqual(StoreRemovalVerification.next(identifiers: [], identifier: identifier, attemptsLeft: 0), .removed)
        XCTAssertEqual(StoreRemovalVerification.next(identifiers: [other], identifier: identifier, attemptsLeft: 0), .removed)
        XCTAssertEqual(StoreRemovalVerification.next(identifiers: [other, identifier], identifier: identifier, attemptsLeft: 10), .retry)
        XCTAssertEqual(StoreRemovalVerification.next(identifiers: [identifier], identifier: identifier, attemptsLeft: 0), .stillPresent)
    }
    func testEveryFallbackExplainsWhyNothingWasSaved() {
        let all: [SessionStorageFallback] = [.insecureOrigin, .unsupportedSystem, .registryUnavailable, .registryChangedElsewhere, .erasedRecordNotUpdated]
        for fallback in all {
            XCTAssertFalse(fallback.userMessage.isEmpty)
            XCTAssertTrue(fallback.userMessage.localizedCaseInsensitiveContains("in memory")
                          || fallback.userMessage.localizedCaseInsensitiveContains("not saved"),
                          "The fallback message does not say the session was not saved: \(fallback.userMessage)")
        }
    }

    func testFallbackMessagesAreDistinctAndNameTheCause() {
        let all: [SessionStorageFallback] = [.insecureOrigin, .unsupportedSystem, .registryUnavailable, .registryChangedElsewhere, .erasedRecordNotUpdated]
        XCTAssertTrue(SessionStorageFallback.insecureOrigin.userMessage.contains("HTTPS"))
        XCTAssertTrue(SessionStorageFallback.unsupportedSystem.userMessage.contains("macOS 14"))
        XCTAssertTrue(SessionStorageFallback.registryUnavailable.userMessage.contains("saved sign-in list"))
        XCTAssertTrue(SessionStorageFallback.registryChangedElsewhere.userMessage.localizedCaseInsensitiveContains("another copy"))
        XCTAssertTrue(SessionStorageFallback.erasedRecordNotUpdated.userMessage.localizedCaseInsensitiveContains("record could not be updated"))
        XCTAssertEqual(Set(all.map(\.userMessage)).count, all.count)
    }
}

@MainActor
final class SessionForgetTests: XCTestCase {
    private func savedSignIn() throws -> (SessionSignIn, SessionStoreRegistry, RecordingEraser, Origin, UUID) {
        let (registry, _) = try makeRegistry()
        let origin = Origin("https://example.com")!
        let identifier = try XCTUnwrap(registry.identifier(for: origin))
        let eraser = RecordingEraser()
        let signIn = SessionSignIn(registry: registry, eraser: eraser, persistentAvailable: true)
        return (signIn, registry, eraser, origin, identifier)
    }

    func testForgetReleasesWebViewsBeforeRemovingData() throws {
        let (signIn, registry, eraser, origin, identifier) = try savedSignIn()
        var events: [String] = []
        var release: (() -> Void)?
        var result: Result<Void, ForgetFailure>?

        signIn.forget(origin: origin, releaseWebViews: { done in
            events.append("release-requested")
            release = done
        }) { outcome in
            events.append("completed:\(outcome)")
            result = outcome
        }

        XCTAssertEqual(events, ["release-requested"])
        XCTAssertTrue(eraser.removed.isEmpty,
                      "WebKit requires the web views to be released before their data store is removed")
        XCTAssertNil(result)
        XCTAssertTrue(registry.contains(origin))

        release?()
        XCTAssertEqual(eraser.removed, [identifier])
        assertForgetSucceeded(result)
        XCTAssertFalse(registry.contains(origin))
        XCTAssertFalse(signIn.hasSavedSignIn(for: origin))
    }

    func testReoptingInAfterForgetAllocatesANewStore() throws {
        let (signIn, registry, _, origin, identifier) = try savedSignIn()
        var result: Result<Void, ForgetFailure>?
        signIn.forget(origin: origin, releaseWebViews: { $0() }) { result = $0 }
        assertForgetSucceeded(result)

        guard case .persistent(let next) = signIn.storage(for: origin, keepSignedIn: true) else {
            return XCTFail("Re-opting in after forget did not allocate a store")
        }
        XCTAssertNotEqual(next, identifier,
                          "A forgotten identifier may still be cached in this process, so it must never be reused")
        XCTAssertEqual(registry.identifierIfPresent(for: origin), next)
    }

    func testErasedStoreIsNeverReopenedWhenTheMappingWriteFails() throws {
        let directory = try temporaryDirectory()
        let file = directory.appendingPathComponent("session-stores.json")
        let manager = FailingFileManager()
        let registry = SessionStoreRegistry(fileURL: file, fileManager: manager)
        let origin = Origin("https://example.com")!
        let identifier = try XCTUnwrap(registry.identifier(for: origin))
        let signIn = SessionSignIn(registry: registry, eraser: RecordingEraser(), persistentAvailable: true)

        // The store erase succeeds (the fake eraser records it), then the
        // mapping write fails. The on-disk record still names the erased store,
        // so this process must never hand that identifier out again.
        manager.failuresRemaining = 1

        var first: Result<Void, ForgetFailure>?
        signIn.forget(origin: origin, releaseWebViews: { $0() }) { first = $0 }
        assertForgetFailed(first, with: .mappingNotSaved)
        XCTAssertTrue(registry.contains(origin), "The record is the only way to retry the forget")
        XCTAssertTrue(signIn.hasSavedSignIn(for: origin))

        XCTAssertEqual(signIn.storage(for: origin, keepSignedIn: true), .ephemeral,
                       "The erased identifier must not be reopened in this process")
        XCTAssertEqual(signIn.fallback(for: origin, keepSignedIn: true), .erasedRecordNotUpdated)

        // Retrying after the write succeeds again finishes the removal, and a
        // later opt-in allocates a fresh identifier instead of the old one.
        var second: Result<Void, ForgetFailure>?
        signIn.forget(origin: origin, releaseWebViews: { $0() }) { second = $0 }
        assertForgetSucceeded(second)
        XCTAssertFalse(registry.contains(origin))
        XCTAssertTrue(signIn.retiredIdentifiers.isEmpty)

        guard case .persistent(let next) = signIn.storage(for: origin, keepSignedIn: true) else {
            return XCTFail("Re-opting in after the retried forget did not allocate a store")
        }
        XCTAssertNotEqual(next, identifier)
    }

    func testForgetFailsClosedWhenAnotherInstanceChangedTheMapping() throws {
        let directory = try temporaryDirectory()
        let (stale, file) = try makeRegistry(in: directory)
        let origin = Origin("https://example.com")!
        _ = try XCTUnwrap(stale.identifier(for: origin))

        let other = SessionStoreRegistry(fileURL: file)
        try other.remove(origin)

        let eraser = RecordingEraser()
        let signIn = SessionSignIn(registry: stale, eraser: eraser, persistentAvailable: true)
        var result: Result<Void, ForgetFailure>?
        signIn.forget(origin: origin, releaseWebViews: { $0() }) { result = $0 }

        assertForgetFailed(result, with: .registryChangedElsewhere)
        XCTAssertTrue(eraser.removed.isEmpty,
                      "A stale instance must not erase an identifier from a superseded snapshot")
    }

    func testRestartReusesTheRetainedRecordAfterAFailedMappingWrite() throws {
        let directory = try temporaryDirectory()
        let file = directory.appendingPathComponent("session-stores.json")
        let manager = FailingFileManager()
        let registry = SessionStoreRegistry(fileURL: file, fileManager: manager)
        let origin = Origin("https://example.com")!
        let identifier = try XCTUnwrap(registry.identifier(for: origin))
        let signIn = SessionSignIn(registry: registry, eraser: RecordingEraser(), persistentAvailable: true)

        manager.failuresRemaining = 1
        var failed: Result<Void, ForgetFailure>?
        signIn.forget(origin: origin, releaseWebViews: { $0() }) { failed = $0 }
        assertForgetFailed(failed, with: .mappingNotSaved)
        XCTAssertTrue(signIn.retiredIdentifiers.contains(identifier))

        // Retirement is process-local. A restart reloads the retained record,
        // so the same identifier is usable again. The store data was reported
        // removed by WebKit before the mapping write failed; the app does not
        // inspect the on-disk bytes, so this is a known, documented boundary.
        let relaunched = SessionStoreRegistry(fileURL: file)
        let restarted = SessionSignIn(registry: relaunched, eraser: RecordingEraser(), persistentAvailable: true)
        XCTAssertTrue(restarted.retiredIdentifiers.isEmpty)
        XCTAssertEqual(restarted.storage(for: origin, keepSignedIn: true), .persistent(identifier))
    }

    func testForgetFailureRetainsMappingForRetry() throws {
        let (signIn, registry, eraser, origin, _) = try savedSignIn()
        eraser.failure = FixtureError()

        var first: Result<Void, ForgetFailure>?
        signIn.forget(origin: origin, releaseWebViews: { $0() }) { first = $0 }
        assertForgetFailed(first, with: .storeStillPresent)
        XCTAssertTrue(registry.contains(origin), "A failed removal must keep the mapping so forget can be retried")
        XCTAssertTrue(signIn.hasSavedSignIn(for: origin))
        let identifier = try XCTUnwrap(registry.identifierIfPresent(for: origin))
        XCTAssertEqual(signIn.storage(for: origin, keepSignedIn: true), .persistent(identifier),
                       "A retained mapping must still select the same saved store")

        eraser.failure = nil
        var second: Result<Void, ForgetFailure>?
        signIn.forget(origin: origin, releaseWebViews: { $0() }) { second = $0 }
        assertForgetSucceeded(second)
        XCTAssertFalse(registry.contains(origin))
    }

    func testForgetWithoutSavedSignInDoesNothing() throws {
        let (registry, _) = try makeRegistry()
        let eraser = RecordingEraser()
        let signIn = SessionSignIn(registry: registry, eraser: eraser, persistentAvailable: true)
        var result: Result<Void, ForgetFailure>?

        signIn.forget(origin: Origin("https://example.com")!, releaseWebViews: { $0() }) { result = $0 }

        assertForgetSucceeded(result)
        XCTAssertTrue(eraser.removed.isEmpty)
    }

    func testForgetRemovesOnlyTheRequestedOrigin() throws {
        let (registry, _) = try makeRegistry()
        let keep = Origin("https://example.com")!
        let forget = Origin("https://example.com:444")!
        let keepIdentifier = try XCTUnwrap(registry.identifier(for: keep))
        _ = try XCTUnwrap(registry.identifier(for: forget))
        let signIn = SessionSignIn(registry: registry, eraser: RecordingEraser(), persistentAvailable: true)

        var result: Result<Void, ForgetFailure>?
        signIn.forget(origin: forget, releaseWebViews: { $0() }) { result = $0 }

        assertForgetSucceeded(result)
        XCTAssertFalse(registry.contains(forget))
        XCTAssertEqual(registry.identifierIfPresent(for: keep), keepIdentifier)
    }

    func testForgetMappingWriteFailureRetainsMappingForRetry() throws {
        let directory = try temporaryDirectory()
        let file = directory.appendingPathComponent("session-stores.json")
        let manager = FailingFileManager()
        let registry = SessionStoreRegistry(fileURL: file, fileManager: manager)
        let origin = Origin("https://example.com")!
        let identifier = try XCTUnwrap(registry.identifier(for: origin))
        let signIn = SessionSignIn(registry: registry, eraser: RecordingEraser(), persistentAvailable: true)

        // A transient write failure keeps the file intact, so a retry can
        // finish without looking like a concurrent modification.
        manager.failuresRemaining = 1

        var first: Result<Void, ForgetFailure>?
        signIn.forget(origin: origin, releaseWebViews: { $0() }) { first = $0 }
        assertForgetFailed(first, with: .mappingNotSaved)
        XCTAssertTrue(registry.contains(origin))
        XCTAssertTrue(signIn.hasSavedSignIn(for: origin))
        XCTAssertEqual(signIn.storage(for: origin, keepSignedIn: true), .ephemeral,
                       "The erased store must be retired after a failed mapping write")
        XCTAssertEqual(signIn.retiredIdentifiers, [identifier])

        var second: Result<Void, ForgetFailure>?
        signIn.forget(origin: origin, releaseWebViews: { $0() }) { second = $0 }
        assertForgetSucceeded(second)
        XCTAssertFalse(registry.contains(origin))
        XCTAssertTrue(signIn.retiredIdentifiers.isEmpty)
    }

    func testForgetWhenRegistryUnavailableReportsFailure() throws {
        let directory = try temporaryDirectory()
        let file = directory.appendingPathComponent("session-stores.json")
        try Data("not json".utf8).write(to: file)
        let registry = SessionStoreRegistry(fileURL: file)
        let eraser = RecordingEraser()
        let signIn = SessionSignIn(registry: registry, eraser: eraser, persistentAvailable: true)
        var result: Result<Void, ForgetFailure>?

        signIn.forget(origin: Origin("https://example.com")!, releaseWebViews: { $0() }) { result = $0 }

        assertForgetFailed(result, with: .registryUnavailable)
        XCTAssertTrue(eraser.removed.isEmpty)
    }
}
