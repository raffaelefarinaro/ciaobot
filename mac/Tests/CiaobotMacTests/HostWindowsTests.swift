import XCTest
import Foundation
import WebKit

@testable import CiaobotMac

@MainActor
private final class HostWindowsHostList: HostListStoring {
    var hosts: [HostRecord]
    init(_ origins: [Origin]) {
        hosts = origins.map { HostRecord(origin: $0.canonicalKey, label: $0.canonicalKey) }
    }
    func add(_ origin: Origin) { }
    func remove(_ origin: Origin) { }
    func rename(_ origin: Origin, label: String) { }
}

@MainActor
private final class HostWindowsEraser: WebsiteDataStoreErasing {
    func remove(identifier: UUID, completion: @escaping (Error?) -> Void) { completion(nil) }
}

private final class HostWindowsResource: NSObject { }

@MainActor
final class MutableHostStoreSource: HostStoreUseSource {
    var value: HostStoreUseSnapshot?
    init(_ value: HostStoreUseSnapshot?) { self.value = value }
    func snapshot() -> HostStoreUseSnapshot? { value }
}

private final class HostWindowsFailingFileManager: FileManager {
    var fail = false
    override func createDirectory(at url: URL, withIntermediateDirectories: Bool, attributes: [FileAttributeKey: Any]? = nil) throws {
        if fail { throw CocoaError(.fileWriteNoPermission) }
        try super.createDirectory(at: url, withIntermediateDirectories: withIntermediateDirectories, attributes: attributes)
    }
}

@MainActor
private final class HostWindowsFakeWindow: HostChatWindowPresenting {
    let capturedBinding: HostChatStoreBinding
    var navigations: [URL] = []
    var shows = 0
    var isClosed = false
    private let onClose: () -> Void
    init(binding: HostChatStoreBinding, onClose: @escaping () -> Void) {
        self.capturedBinding = binding
        self.onClose = onClose
    }
    func navigate(to url: URL) { navigations.append(url) }
    func show() { shows += 1 }
    func close() {
        guard !isClosed else { return }
        isClosed = true
        onClose()
    }
}

@MainActor
private final class HostWindowsFakeBuilder: HostChatWindowBuilding {
    private(set) var windows: [HostWindowsFakeWindow] = []
    private(set) var requests: [URLRequest] = []
    private(set) var resources: [HostWindowsResource] = []
    func releaseResources() { resources.removeAll() }
    func makeWindow(origin: Origin, binding: HostChatStoreBinding, store: WKWebsiteDataStore,
                    initialRequest: URLRequest, tracking: HostStoreUseTracking,
                    onClose: @escaping () -> Void) -> HostChatWindowPresenting {
        requests.append(initialRequest)
        let resource = HostWindowsResource()
        resources.append(resource)
        tracking.attach(resource, for: origin, binding: binding)
        let window = HostWindowsFakeWindow(binding: binding, onClose: onClose)
        windows.append(window)
        return window
    }
}

@MainActor
private final class HostWindowsCountingResolver: HostChatStoreResolving {
    var bindingValue: HostChatStoreBinding? = .loopbackEphemeral
    var bindingCalls = 0
    var storeCalls = 0
    var duringStore: (() -> Void)?
    func binding(for origin: Origin) -> HostChatStoreBinding? { bindingCalls += 1; return bindingValue }
    func makeStore(for origin: Origin, binding: HostChatStoreBinding) -> WKWebsiteDataStore? {
        storeCalls += 1
        duringStore?()
        return .nonPersistent()
    }
}

@MainActor
final class HostWindowsTests: XCTestCase {
    func testResolverRequiresSavedHostAndVerifiedStoreWithoutAllocatingMapping() throws {
        let directory = try temporaryDirectory()
        let (registry, file) = try makeRegistry(in: directory)
        let remote = try XCTUnwrap(Origin("https://saved.example"))
        let unsaved = try XCTUnwrap(Origin("https://unsaved.example"))
        let identifier = try XCTUnwrap(registry.identifier(for: remote))
        let before = try Data(contentsOf: file)
        let signIn = SessionSignIn(registry: registry, eraser: HostWindowsEraser(), persistentAvailable: true)
        let resolver = HostChatStoreResolver(
            hosts: HostWindowsHostList([remote]),
            signIn: signIn,
            ephemeralStoreFactory: { .nonPersistent() },
            persistentStoreFactory: { .init(forIdentifier: $0) }
        )

        XCTAssertEqual(resolver.binding(for: remote), HostChatStoreBinding.saved(identifier))
        XCTAssertNil(resolver.binding(for: unsaved))
        XCTAssertEqual(try Data(contentsOf: file), before)
        let unmapped = try XCTUnwrap(Origin("https://unmapped.example"))
        let unsupported = HostChatStoreResolver(hosts: HostWindowsHostList([remote]), signIn: SessionSignIn(registry: registry, eraser: HostWindowsEraser(), persistentAvailable: false))
        XCTAssertNil(unsupported.binding(for: remote))
        XCTAssertNil(HostChatStoreResolver(hosts: HostWindowsHostList([unmapped]), signIn: signIn).binding(for: unmapped))
        var calls = 0
        let failing = HostChatStoreResolver(hosts: HostWindowsHostList([remote]), signIn: signIn, persistentStoreFactory: { _ in calls += 1; return nil })
        XCTAssertNil(failing.makeStore(for: remote, binding: .saved(identifier)))
        XCTAssertEqual(calls, 1)
        let other = SessionStoreRegistry(fileURL: file)
        try other.remove(remote)
        XCTAssertNil(failing.makeStore(for: remote, binding: .saved(identifier)))
        XCTAssertEqual(calls, 1, "Stale binding must not reach the factory")

        let retiredFile = directory.appendingPathComponent("retired.json")
        let fm = HostWindowsFailingFileManager()
        let retiredRegistry = SessionStoreRegistry(fileURL: retiredFile, fileManager: fm)
        let retiredID = try XCTUnwrap(retiredRegistry.identifier(for: remote))
        let retiredSignIn = SessionSignIn(registry: retiredRegistry, eraser: HostWindowsEraser(), persistentAvailable: true)
        fm.fail = true
        retiredSignIn.forget(origin: remote, releaseWebViews: { $0() }, completion: { _ in })
        XCTAssertTrue(retiredSignIn.retiredIdentifiers.contains(retiredID))
        XCTAssertNil(HostChatStoreResolver(hosts: HostWindowsHostList([remote]), signIn: retiredSignIn).binding(for: remote))
    }

    func testResolverAllowsSavedLoopbackWithEphemeralStore() throws {
        let (registry, file) = try makeRegistry()
        let loopback = try XCTUnwrap(Origin("http://127.0.0.1:8443"))
        let before = FileManager.default.fileExists(atPath: file.path) ? try Data(contentsOf: file) : nil
        let signIn = SessionSignIn(registry: registry, eraser: HostWindowsEraser(), persistentAvailable: true)
        let resolver = HostChatStoreResolver(
            hosts: HostWindowsHostList([loopback]),
            signIn: signIn,
            ephemeralStoreFactory: { .nonPersistent() },
            persistentStoreFactory: { .init(forIdentifier: $0) }
        )

        XCTAssertEqual(resolver.binding(for: loopback), HostChatStoreBinding.loopbackEphemeral)
        XCTAssertNotNil(resolver.makeStore(for: loopback, binding: .loopbackEphemeral))
        XCTAssertEqual(FileManager.default.fileExists(atPath: file.path) ? try Data(contentsOf: file) : nil, before)
    }

    func testClosedWindowResourcesRemainTrackedUntilDeallocation() throws {
        let registry = HostStoreUseRegistry()
        let first = try XCTUnwrap(Origin("https://first.example"))
        let second = try XCTUnwrap(Origin("https://second.example"))
        let bindingA = HostChatStoreBinding.saved(UUID())
        var resource: HostWindowsResource? = HostWindowsResource()
        var another: HostWindowsResource? = HostWindowsResource()
        registry.attach(another!, for: first, binding: bindingA)
        registry.attach(resource!, for: first, binding: bindingA)
        registry.attach(HostWindowsResource(), for: second, binding: bindingA)
        XCTAssertFalse(registry.allReleased(for: first))
        XCTAssertTrue(registry.allReleased(for: second))

        var timedOut: Bool?
        registry.whenAllReleased(for: first, timeout: 0, completion: { timedOut = $0 })
        XCTAssertEqual(timedOut, false)

        resource = nil
        XCTAssertFalse(registry.allReleased(for: first))
        another = nil
        var released: Bool?
        registry.whenAllReleased(for: first, timeout: 0.2, completion: { released = $0 })
        XCTAssertEqual(released, true)
    }

    func testOperationSurvivesDroppedCallbackUntilExplicitFinish() throws {
        let tracker = HostStoreUseRegistry()
        let origin = try XCTUnwrap(Origin("https://operation.example"))
        let id = try XCTUnwrap(tracker.beginOperation(snapshot: .init(origin: origin, store: .nonPersistent(), identity: .ephemeral(UUID()))))
        XCTAssertFalse(tracker.allReleased(for: origin))
        XCTAssertTrue(tracker.claimCompletion(id: id))
        XCTAssertFalse(tracker.claimCompletion(id: id))
        tracker.finishOperation(id: id)
        tracker.finishOperation(id: id)
        XCTAssertTrue(tracker.allReleased(for: origin))
    }

    func testOperationCompletionClaimsOnceAndFinishIsIdempotent() throws {
        let registry = HostStoreUseRegistry()
        let origin = try XCTUnwrap(Origin("https://claim.example"))
        let snapshot = HostStoreUseSnapshot(origin: origin, store: .nonPersistent(), identity: .ephemeral(UUID()))
        let first = try XCTUnwrap(registry.beginOperation(snapshot: snapshot))
        let second = try XCTUnwrap(registry.beginOperation(snapshot: snapshot))
        XCTAssertTrue(registry.claimCompletion(id: first))
        XCTAssertFalse(registry.claimCompletion(id: first))
        registry.finishOperation(id: first)
        registry.finishOperation(id: first)
        XCTAssertFalse(registry.allReleased(for: origin))
        XCTAssertTrue(registry.claimCompletion(id: second))
        registry.finishOperation(id: second)
        XCTAssertTrue(registry.allReleased(for: origin))
    }

    func testTypedIdentityCompatibilityAndUnknownResources() throws {
        let registry = HostStoreUseRegistry()
        let origin = try XCTUnwrap(Origin("https://identity.example"))
        let persistent = UUID()
        let first = NSObject()
        registry.attach(first, for: origin, identity: .persistent(persistent))
        XCTAssertFalse(registry.hasConflictingResources(for: origin, identity: .persistent(persistent)))
        XCTAssertTrue(registry.hasConflictingResources(for: origin, identity: .persistent(UUID())))
        XCTAssertTrue(registry.hasConflictingResources(for: origin, identity: .ephemeral(UUID())))
        let other = try XCTUnwrap(Origin("https://ephemeral.example"))
        let ephemeral = NSObject()
        let a = HostStoreIdentity.ephemeral(UUID())
        let b = HostStoreIdentity.ephemeral(UUID())
        XCTAssertNotEqual(a, b)
        registry.attach(ephemeral, for: other, identity: a)
        XCTAssertFalse(registry.hasConflictingResources(for: other, identity: b))
        XCTAssertFalse(registry.allReleased(for: other))
        let unknown = NSObject()
        registry.attach(unknown, for: other, identity: .unknown)
        XCTAssertTrue(registry.hasConflictingResources(for: other, identity: b))
        withExtendedLifetime((first, ephemeral, unknown)) {}
    }

    func testOperationsRemainVisibleAcrossTimeoutAndDifferentBindings() throws {
        let tracker = HostStoreUseRegistry()
        let origin = try XCTUnwrap(Origin("https://operation.example"))
        let other = try XCTUnwrap(Origin("https://other.example"))
        var ids: [UUID] = []
        for _ in 0..<3 {
            ids.append(try XCTUnwrap(tracker.beginOperation(snapshot: .init(origin: origin, store: .nonPersistent(), identity: .ephemeral(UUID())))))
        }
        for _ in 0..<3 {
            XCTAssertNil(tracker.beginOperation(snapshot: .init(origin: origin, store: .nonPersistent(), identity: .ephemeral(UUID()))))
        }
        var released: Bool?
        tracker.whenAllReleased(for: origin, timeout: 0) { released = $0 }
        XCTAssertEqual(released, false)
        XCTAssertFalse(tracker.allReleased(for: origin))
        let otherID = try XCTUnwrap(tracker.beginOperation(snapshot: .init(origin: other, store: .nonPersistent(), identity: .persistent(UUID()))))
        XCTAssertTrue(tracker.claimCompletion(id: ids[0]))
        tracker.finishOperation(id: ids[0])
        XCTAssertFalse(tracker.allReleased(for: origin))
        XCTAssertNotNil(tracker.beginOperation(snapshot: .init(origin: origin, store: .nonPersistent(), identity: .persistent(UUID()))))
        XCTAssertNil(tracker.beginOperation(snapshot: .init(origin: origin, store: .nonPersistent(), identity: .ephemeral(UUID()))))
        tracker.finishOperation(id: otherID)
        XCTAssertTrue(tracker.allReleased(for: other))
    }

    func testSourceRejectsDisconnectedChangedOriginChangedStoreAndExcludedOrigin() throws {
        let origin = try XCTUnwrap(Origin("https://source.example"))
        let other = try XCTUnwrap(Origin("https://other.example"))
        let store = WKWebsiteDataStore.nonPersistent()
        let otherStore = WKWebsiteDataStore.nonPersistent()
        let source = MutableHostStoreSource(.init(origin: origin, store: store, identity: .ephemeral(UUID())))
        let registry = HostStoreUseRegistry()
        let excluded = HostStoreTrackingBundle(source: source, registry: registry, isExcluded: { $0 == origin })
        XCTAssertEqual(excluded.acquire(origin: origin, store: store), .refused)
        let bundle = HostStoreTrackingBundle(source: source, registry: registry)
        source.value = nil
        XCTAssertEqual(bundle.acquire(origin: origin, store: store), .refused)
        source.value = .init(origin: other, store: store, identity: .ephemeral(UUID()))
        XCTAssertEqual(bundle.acquire(origin: origin, store: store), .refused)
        source.value = .init(origin: origin, store: otherStore, identity: .ephemeral(UUID()))
        XCTAssertEqual(bundle.acquire(origin: origin, store: store), .refused)
    }

    func testOpenValidatesCanonicalOriginAndChatIDBeforeResolvingStore() throws {
        let resolver = HostWindowsCountingResolver()
        let builder = HostWindowsFakeBuilder()
        let manager = HostChatWindowManager(resolver: resolver, builder: builder)
        XCTAssertFalse(manager.canOpen(originKey: "HTTP://127.0.0.1:8080"))
        XCTAssertFalse(manager.open(originKey: "http://127.0.0.1:8080", chatID: "../escape"))
        XCTAssertEqual(resolver.bindingCalls, 0)
        XCTAssertEqual(resolver.storeCalls, 0)
        XCTAssertTrue(builder.windows.isEmpty)
    }

    func testManagerOpensOneWindowPerHostAndNavigatesToLatestChat() throws {
        let first = try XCTUnwrap(Origin("http://127.0.0.1:8443"))
        let second = try XCTUnwrap(Origin("http://127.0.0.1:8444"))
        let resolver = HostWindowsCountingResolver()
        let builder = HostWindowsFakeBuilder()
        let manager = HostChatWindowManager(resolver: resolver, builder: builder)

        XCTAssertTrue(manager.open(originKey: first.canonicalKey, chatID: "chat-a"))
        XCTAssertTrue(manager.open(originKey: first.canonicalKey, chatID: "chat-b"))
        XCTAssertTrue(manager.open(originKey: second.canonicalKey, chatID: "chat-c"))
        XCTAssertEqual(builder.windows.count, 2)
        XCTAssertEqual(builder.requests.first?.url, ChatLink.url(origin: first, chatID: "chat-a"))
        XCTAssertEqual(builder.windows.first?.navigations, [ChatLink.url(origin: first, chatID: "chat-b")!])
        XCTAssertEqual(builder.windows.first?.shows, 2)
    }

    func testBindingChangeClosesOldWindowAndWaitsForOldResources() throws {
        let origin = try XCTUnwrap(Origin("http://127.0.0.1:8443"))
        let resolver = HostWindowsCountingResolver()
        let oldBinding = HostChatStoreBinding.saved(UUID())
        let newBinding = HostChatStoreBinding.saved(UUID())
        resolver.bindingValue = oldBinding
        let builder = HostWindowsFakeBuilder()
        let tracking = HostStoreUseRegistry()
        let manager = HostChatWindowManager(resolver: resolver, tracking: tracking, builder: builder)

        autoreleasepool { XCTAssertTrue(manager.open(originKey: origin.canonicalKey, chatID: "first")) }
        resolver.bindingValue = newBinding
        XCTAssertFalse(manager.open(originKey: origin.canonicalKey, chatID: "second"))
        XCTAssertTrue(builder.windows[0].isClosed)
        resolver.bindingValue = newBinding
        XCTAssertFalse(manager.open(originKey: origin.canonicalKey, chatID: "second"))
        XCTAssertEqual(builder.windows[0].capturedBinding, oldBinding)
        XCTAssertEqual(builder.windows.count, 1)
        builder.releaseResources()
        XCTAssertFalse(tracking.hasConflictingResources(for: origin, binding: newBinding))
        XCTAssertTrue(manager.open(originKey: origin.canonicalKey, chatID: "second"))
        XCTAssertEqual(builder.windows.count, 2)
        XCTAssertEqual(builder.windows.last?.capturedBinding, newBinding)
    }

    func testConflictingResourceAppearingDuringStoreFactoryPreventsWindowConstruction() throws {
        let origin = try XCTUnwrap(Origin("https://host.example"))
        let resolver = HostWindowsCountingResolver()
        resolver.bindingValue = .saved(UUID())
        let tracker = HostStoreUseRegistry()
        let conflicting = HostWindowsResource()
        resolver.duringStore = { tracker.attach(conflicting, for: origin, binding: .saved(UUID())) }
        let builder = HostWindowsFakeBuilder()
        let manager = HostChatWindowManager(resolver: resolver, tracking: tracker, builder: builder)
        XCTAssertFalse(manager.open(originKey: origin.canonicalKey, chatID: "first"))
        XCTAssertEqual(resolver.storeCalls, 1)
        XCTAssertTrue(builder.windows.isEmpty)
        XCTAssertTrue(builder.requests.isEmpty)
        withExtendedLifetime(conflicting) {}
    }

    func testMissingBindingClosesStaleWindow() throws {
        let origin = try XCTUnwrap(Origin("http://127.0.0.1:8443"))
        let resolver = HostWindowsCountingResolver()
        let builder = HostWindowsFakeBuilder()
        let manager = HostChatWindowManager(resolver: resolver, builder: builder)
        XCTAssertTrue(manager.open(originKey: origin.canonicalKey, chatID: "first"))
        resolver.bindingValue = nil
        XCTAssertFalse(manager.open(originKey: origin.canonicalKey, chatID: "second"))
        XCTAssertTrue(builder.windows[0].isClosed)
    }

    func testUserClosedWindowBindingAThenBindingBWaitsForOldView() throws {
        let origin = try XCTUnwrap(Origin("http://127.0.0.1:8443"))
        let resolver = HostWindowsCountingResolver()
        resolver.bindingValue = .saved(UUID())
        let builder = HostWindowsFakeBuilder()
        let manager = HostChatWindowManager(resolver: resolver, builder: builder)
        autoreleasepool { XCTAssertTrue(manager.open(originKey: origin.canonicalKey, chatID: "first")) }
        resolver.bindingValue = .saved(UUID())
        builder.windows[0].close()
        XCTAssertFalse(manager.open(originKey: origin.canonicalKey, chatID: "second"))
        builder.releaseResources()
        XCTAssertTrue(manager.open(originKey: origin.canonicalKey, chatID: "second"))
        XCTAssertEqual(builder.windows.count, 2)
    }

    func testProductionWebViewBuilderTracksAndTearsDownWithoutWindow() throws {
        let origin = try XCTUnwrap(Origin("http://127.0.0.1:8443"))
        let tracker = HostStoreUseRegistry()
        func constructAndRelease() throws {
            let store = WKWebsiteDataStore.nonPersistent()
            let construction = HostChatWebViewConstruction(origin: origin, binding: .loopbackEphemeral,
                                                           store: store, tracking: tracker)
            let view = try XCTUnwrap(construction.webView)
            XCTAssertTrue(view.configuration.websiteDataStore === store)
            XCTAssertEqual(construction.coordinator?.navigationProfile, .hostChat)
            XCTAssertFalse(tracker.allReleased(for: origin))
            construction.tearDown()
            XCTAssertFalse(view.isLoading)
            XCTAssertNil(view.navigationDelegate)
            XCTAssertNil(view.uiDelegate)
            XCTAssertFalse(tracker.allReleased(for: origin))
        }
        try autoreleasepool { try constructAndRelease() }
        let released = expectation(description: "WebView released")
        tracker.whenAllReleased(for: origin, timeout: 5) { result in
            XCTAssertTrue(result)
            released.fulfill()
        }
        wait(for: [released], timeout: 6)
    }
}
