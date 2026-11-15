import Foundation
import WebKit
import AppKit

@MainActor
protocol HostChatWindowPresenting: AnyObject {
    var capturedBinding: HostChatStoreBinding { get }
    func navigate(to url: URL)
    func show()
    func close()
}

@MainActor
protocol HostChatWindowBuilding: AnyObject {
    func makeWindow(
        origin: Origin,
        binding: HostChatStoreBinding,
        store: WKWebsiteDataStore,
        initialRequest: URLRequest,
        tracking: HostStoreUseTracking,
        onClose: @escaping () -> Void
    ) -> HostChatWindowPresenting
}

@MainActor
protocol HostChatWindowManaging: AnyObject {
    func canOpen(originKey: String) -> Bool
    func open(originKey: String, chatID: String) -> Bool
}

@MainActor
final class HostChatWebViewConstruction {
    let binding: HostChatStoreBinding
    private(set) var webView: WKWebView?
    private(set) var coordinator: WebWindow.Coordinator?

    init(origin: Origin, binding: HostChatStoreBinding, store: WKWebsiteDataStore, tracking: HostStoreUseTracking) {
        self.binding = binding
        let coordinator = WebWindow.Coordinator(
            origin: origin,
            onFailure: nil,
            downloadFailure: { _ in },
            filePanel: { _, completion in completion(nil) },
            downloadDestination: { _, completion in completion(nil) },
            openExternal: { _ in },
            navigationProfile: .hostChat
        )
        let view = WebWindow.Coordinator.makeWebView(
            origin: origin,
            coordinator: coordinator,
            websiteDataStore: store
        )
        self.coordinator = coordinator
        self.webView = view
        tracking.attach(view, for: origin, binding: binding)
    }

    func tearDown() {
        guard let webView else { return }
        coordinator?.prepareForRelease()
        webView.stopLoading()
        webView.navigationDelegate = nil
        webView.uiDelegate = nil
        self.webView = nil
        coordinator = nil
    }
}

@MainActor
final class HostChatWindowController: NSObject, HostChatWindowPresenting, NSWindowDelegate {
    let capturedBinding: HostChatStoreBinding
    private let origin: Origin
    private var construction: HostChatWebViewConstruction?
    private var window: NSWindow?
    private let onClose: () -> Void

    init(origin: Origin, binding: HostChatStoreBinding, store: WKWebsiteDataStore, initialRequest: URLRequest,
         tracking: HostStoreUseTracking, onClose: @escaping () -> Void) {
        self.origin = origin
        self.capturedBinding = binding
        self.onClose = onClose
        let construction = HostChatWebViewConstruction(origin: origin, binding: binding, store: store, tracking: tracking)
        self.construction = construction
        super.init()
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 900, height: 700), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = origin.url.host ?? origin.canonicalKey
        window.delegate = self
        window.contentView = construction.webView
        self.window = window
        if let url = initialRequest.url { construction.coordinator?.loadHostChat(url) }
    }

    func navigate(to url: URL) {
        construction?.coordinator?.loadHostChat(url)
    }

    func show() {
        guard let window else { return }
        NSApplication.shared.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    func close() {
        guard let window else { return }
        window.close()
        if self.window != nil {
            tearDown()
            onClose()
        }
    }

    func windowWillClose(_ notification: Notification) {
        tearDown()
        onClose()
    }

    private func tearDown() {
        construction?.tearDown()
        construction = nil
        window?.delegate = nil
        window?.contentView = nil
        window = nil
    }
}

@MainActor
final class HostChatWindowBuilder: HostChatWindowBuilding {
    func makeWindow(origin: Origin, binding: HostChatStoreBinding, store: WKWebsiteDataStore,
                    initialRequest: URLRequest, tracking: HostStoreUseTracking,
                    onClose: @escaping () -> Void) -> HostChatWindowPresenting {
        HostChatWindowController(origin: origin, binding: binding, store: store, initialRequest: initialRequest,
                                 tracking: tracking, onClose: onClose)
    }
}

@MainActor
final class HostChatWindowManager: HostChatWindowManaging {
    private struct Entry {
        let token: UUID
        let window: HostChatWindowPresenting
        let binding: HostChatStoreBinding?
        var lastURL: URL
    }

    private let resolver: HostChatStoreResolving
    private let tracking: HostStoreUseTracking
    private let builder: HostChatWindowBuilding
    private var windows: [String: Entry] = [:]

    init(resolver: HostChatStoreResolving, tracking: HostStoreUseTracking? = nil, builder: HostChatWindowBuilding? = nil) {
        self.resolver = resolver
        self.tracking = tracking ?? HostStoreUseRegistry()
        self.builder = builder ?? HostChatWindowBuilder()
    }

    func canOpen(originKey: String) -> Bool {
        guard let origin = canonicalOrigin(originKey), resolver.binding(for: origin) != nil else { return false }
        return true
    }

    func open(originKey: String, chatID: String) -> Bool {
        guard let origin = canonicalOrigin(originKey), ChatLink.isValidChatID(chatID),
              let url = ChatLink.url(origin: origin, chatID: chatID) else { return false }
        let key = origin.canonicalKey
        let binding = resolver.binding(for: origin)
        if let existing = windows[key] {
            guard binding == existing.binding else {
                existing.window.close()
                if windows[key]?.token == existing.token { windows.removeValue(forKey: key) }
                return false
            }
            guard let binding, !tracking.hasConflictingResources(for: origin, binding: binding) else { return false }
            if existing.lastURL != url {
                existing.window.navigate(to: url)
                windows[key]?.lastURL = url
            }
            existing.window.show()
            return true
        }
        guard let binding, !tracking.hasConflictingResources(for: origin, binding: binding),
              let store = resolver.makeStore(for: origin, binding: binding),
              !tracking.hasConflictingResources(for: origin, binding: binding) else { return false }
        let token = UUID()
        let window = builder.makeWindow(
            origin: origin,
            binding: binding,
            store: store,
            initialRequest: URLRequest(url: url),
            tracking: tracking,
            onClose: { [weak self] in
                guard self?.windows[key]?.token == token else { return }
                self?.windows.removeValue(forKey: key)
            }
        )
        windows[key] = Entry(token: token, window: window, binding: binding, lastURL: url)
        window.show()
        return true
    }

    private func canonicalOrigin(_ key: String) -> Origin? {
        guard let origin = Origin(key), origin.canonicalKey == key else { return nil }
        return origin
    }
}

enum HostChatStoreBinding: Equatable {
    case saved(UUID)
    case loopbackEphemeral
    case activeSession
}

enum HostStoreIdentity: Equatable {
    case persistent(UUID)
    case ephemeral(UUID)
    case none
    case unknown

    func isCompatible(with other: HostStoreIdentity) -> Bool {
        switch (self, other) {
        case let (.persistent(a), .persistent(b)): a == b
        case (.ephemeral, .ephemeral), (.none, .none): true
        default: false
        }
    }
}

struct HostStoreUseSnapshot {
    let origin: Origin
    let store: WKWebsiteDataStore?
    let identity: HostStoreIdentity
}

@MainActor
protocol HostStoreUseSource: AnyObject {
    func snapshot() -> HostStoreUseSnapshot?
}

struct HostStoreTrackingBundle {
    let source: HostStoreUseSource
    let registry: HostStoreUseRegistry
    let isExcluded: (Origin) -> Bool

    @MainActor
    init(source: HostStoreUseSource, registry: HostStoreUseRegistry, isExcluded: @escaping (Origin) -> Bool = { _ in false }) {
        self.source = source
        self.registry = registry
        self.isExcluded = isExcluded
    }

    @MainActor
    func acquire(origin: Origin, store: WKWebsiteDataStore?) -> HostStoreUseAcquisition {
        guard !isExcluded(origin), let snapshot = source.snapshot(), snapshot.origin.canonicalKey == origin.canonicalKey,
              Self.sameStore(snapshot.store, store),
              (snapshot.store != nil || snapshot.identity == .none),
              (snapshot.store == nil || snapshot.identity != .none) else { return .refused }
        guard let store = snapshot.store else { return .noStore }
        guard let id = registry.beginOperation(snapshot: HostStoreUseSnapshot(origin: origin, store: store, identity: snapshot.identity)) else { return .refused }
        return .operation(id)
    }

    private static func sameStore(_ lhs: WKWebsiteDataStore?, _ rhs: WKWebsiteDataStore?) -> Bool {
        switch (lhs, rhs) { case (nil, nil): true; case let (a?, b?): a === b; default: false }
    }
}

enum HostStoreUseAcquisition: Equatable { case refused, noStore, operation(UUID) }

@MainActor
protocol HostChatStoreResolving: AnyObject {
    func binding(for origin: Origin) -> HostChatStoreBinding?
    func makeStore(for origin: Origin, binding: HostChatStoreBinding) -> WKWebsiteDataStore?
}

@MainActor
protocol HostStoreUseTracking: AnyObject {
    func attach(_ resource: AnyObject, for origin: Origin, binding: HostChatStoreBinding)
    func allReleased(for origin: Origin) -> Bool
    func hasConflictingResources(for origin: Origin, binding: HostChatStoreBinding) -> Bool
    func whenAllReleased(for origin: Origin, timeout: TimeInterval, completion: @escaping (Bool) -> Void)
}

@MainActor
final class HostStoreUseRegistry: HostStoreUseTracking {
    private final class Entry {
        weak var resource: AnyObject?
        let binding: HostChatStoreBinding?
        let identity: HostStoreIdentity?
        init(_ resource: AnyObject, binding: HostChatStoreBinding? = nil, identity: HostStoreIdentity? = nil) {
            self.resource = resource
            self.binding = binding
            self.identity = identity
        }
    }

    private var entries: [String: [Entry]] = [:]
    private struct Operation { let origin: String; let identity: HostStoreIdentity; var processing: Bool }
    private var operations: [UUID: Operation] = [:]

    func beginOperation(snapshot: HostStoreUseSnapshot) -> UUID? {
        let key = snapshot.origin.canonicalKey
        guard snapshot.store != nil, snapshot.identity != .none, snapshot.identity != .unknown else { return nil }
        guard operations.values.filter({ $0.origin == key }).count < 3 else { return nil }
        let id = UUID()
        operations[id] = Operation(origin: key, identity: snapshot.identity, processing: false)
        return id
    }

    func claimCompletion(id: UUID) -> Bool {
        guard var operation = operations[id], !operation.processing else { return false }
        operation.processing = true
        operations[id] = operation
        return true
    }

    func finishOperation(id: UUID) { operations.removeValue(forKey: id) }
    func attach(_ resource: AnyObject, for origin: Origin, binding: HostChatStoreBinding) {
        prune(origin.canonicalKey)
        entries[origin.canonicalKey, default: []].append(Entry(resource, binding: binding))
    }

    func attach(_ resource: AnyObject, for origin: Origin, identity: HostStoreIdentity) {
        prune(origin.canonicalKey)
        entries[origin.canonicalKey, default: []].append(Entry(resource, identity: identity))
    }

    func allReleased(for origin: Origin) -> Bool {
        prune(origin.canonicalKey)
        return entries[origin.canonicalKey, default: []].isEmpty && !operations.values.contains { $0.origin == origin.canonicalKey }
    }

    func hasConflictingResources(for origin: Origin, binding: HostChatStoreBinding) -> Bool {
        prune(origin.canonicalKey)
        return entries[origin.canonicalKey, default: []].contains { $0.binding != binding }
            || operations.values.contains { $0.origin == origin.canonicalKey }
    }

    func hasConflictingResources(for origin: Origin, identity: HostStoreIdentity) -> Bool {
        prune(origin.canonicalKey)
        return entries[origin.canonicalKey, default: []].contains { entry in
            guard let known = entry.identity else { return true }
            return !known.isCompatible(with: identity)
        } || operations.values.contains { $0.origin == origin.canonicalKey && !$0.identity.isCompatible(with: identity) }
    }

    func whenAllReleased(for origin: Origin, timeout: TimeInterval, completion: @escaping (Bool) -> Void) {
        let deadline = Date().addingTimeInterval(max(0, timeout))
        func poll() {
            if allReleased(for: origin) { completion(true) }
            else if Date() >= deadline { completion(false) }
            else {
                Task { @MainActor in
                    try? await Task.sleep(nanoseconds: 50_000_000)
                    poll()
                }
            }
        }
        poll()
    }

    private func prune(_ key: String) {
        entries[key]?.removeAll { $0.resource == nil }
        if entries[key]?.isEmpty == true { entries.removeValue(forKey: key) }
    }
}

@MainActor
final class HostChatStoreResolver: HostChatStoreResolving {
    private let hosts: HostListStoring
    private let signIn: SessionSignIn
    private let ephemeralStoreFactory: @MainActor () -> WKWebsiteDataStore?
    private let persistentStoreFactory: @MainActor (UUID) -> WKWebsiteDataStore?

    init(
        hosts: HostListStoring,
        signIn: SessionSignIn,
        ephemeralStoreFactory: @escaping @MainActor () -> WKWebsiteDataStore? = { .nonPersistent() },
        persistentStoreFactory: @escaping @MainActor (UUID) -> WKWebsiteDataStore? = { identifier in
            guard #available(macOS 14.0, *) else { return nil }
            return WKWebsiteDataStore(forIdentifier: identifier)
        }
    ) {
        self.hosts = hosts
        self.signIn = signIn
        self.ephemeralStoreFactory = ephemeralStoreFactory
        self.persistentStoreFactory = persistentStoreFactory
    }

    func binding(for origin: Origin) -> HostChatStoreBinding? {
        guard hosts.hosts.contains(where: { $0.origin == origin.canonicalKey }) else { return nil }
        if origin.isLoopback { return .loopbackEphemeral }
        guard origin.usesTLS, signIn.persistentAvailable,
              let identifier = signIn.registry.verifiedIdentifierIfPresent(for: origin),
              !signIn.retiredIdentifiers.contains(identifier) else { return nil }
        return .saved(identifier)
    }

    func makeStore(for origin: Origin, binding: HostChatStoreBinding) -> WKWebsiteDataStore? {
        guard self.binding(for: origin) == binding else { return nil }
        switch binding {
        case .saved(let identifier): return persistentStoreFactory(identifier)
        case .loopbackEphemeral: return ephemeralStoreFactory()
        case .activeSession: return nil
        }
    }
}
