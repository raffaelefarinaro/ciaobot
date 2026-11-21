import Foundation
import XCTest

@testable import CiaobotMac

/// A tray switch runs the connection screen's own probe, teardown and session
/// build. These tests drive the screen with an injected probe and a temporary
/// sign-in registry, so no test contacts a server or touches real defaults.
@MainActor
final class ServerSwitchConnectionTests: XCTestCase {
    private final class SwitchProbe: ProbeChecking {
        private let answers: (String) -> Bool
        private(set) var checked: [String] = []

        init(answers: @escaping (String) -> Bool) { self.answers = answers }

        func check(_ origin: Origin, completion: @escaping (Bool) -> Void) {
            checked.append(origin.canonicalKey)
            completion(answers(origin.canonicalKey))
        }
    }

    private final class SwitchEraser: WebsiteDataStoreErasing {
        func remove(identifier: UUID, completion: @escaping (Error?) -> Void) {
            completion(nil)
        }
    }

    private let local = "http://127.0.0.1:8443"
    private let remote = "https://vps.example:8443"
    private let other = "https://other.example:8443"

    private var suiteName = ""
    private var defaults: UserDefaults!
    private var lastServer: UserDefaultsLastServerStore!
    private var hostStore: UserDefaultsHostListStore!
    private var registryURL: URL!

    override func setUp() {
        super.setUp()
        suiteName = IsolatedDefaults.makeName("ServerSwitchConnection")
        defaults = UserDefaults(suiteName: suiteName)
        defaults.removePersistentDomain(forName: suiteName)
        lastServer = UserDefaultsLastServerStore(defaults: defaults)
        hostStore = UserDefaultsHostListStore(defaults: defaults, lastServer: lastServer)
        registryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("ServerSwitch-Registry-\(UUID().uuidString).json")
    }

    override func tearDown() {
        IsolatedDefaults.remove(suiteName)
        if let registryURL { try? FileManager.default.removeItem(at: registryURL) }
        super.tearDown()
    }

    private func makeView(
        probe: ProbeChecking,
        session: AppSessionStatus,
        engineMode: EngineMode = .external,
        modeStore: EngineModeStoring? = nil,
        modeSwitchDialog: (@MainActor (EngineLifecycle.ModeSwitchDialog) -> Bool)? = nil
    ) -> ConnectionView {
        ConnectionView(
            probe: probe,
            signIn: SessionSignIn(
                registry: SessionStoreRegistry(fileURL: registryURL),
                eraser: SwitchEraser(),
                persistentAvailable: false
            ),
            session: session,
            lastServer: lastServer,
            hostStore: hostStore,
            engineMode: engineMode,
            modeStore: modeStore,
            modeSwitchDialog: modeSwitchDialog,
            hostIsArm64: false
        )
    }

    /// Connects the screen to `origin` the way a first connection does, so a
    /// later tray switch has a live session to keep or replace.
    private func connect(_ origin: String, probe: SwitchProbe, session: AppSessionStatus) throws -> ConnectionView {
        lastServer.remember(try XCTUnwrap(Origin(origin)))
        let view = makeView(probe: probe, session: session)
        view.connect()
        XCTAssertEqual(session.currentOrigin?.canonicalKey, origin)
        return view
    }

    func testATrayHostSwitchProbesOnceAndReplacesTheSessionOnlyAfterTheAnswer() throws {
        let probe = SwitchProbe { _ in true }
        let session = AppSessionStatus()
        let view = try connect(remote, probe: probe, session: session)

        view.switchServer(to: .host(try XCTUnwrap(Origin(other))))

        XCTAssertEqual(probe.checked, [remote, other], "The switch probed the new host exactly once")
        XCTAssertEqual(session.currentOrigin?.canonicalKey, other)
        XCTAssertEqual(lastServer.savedOrigin?.canonicalKey, other, "The answering host becomes the launch target")
        XCTAssertTrue(hostStore.hosts.contains { $0.origin == other })
    }

    func testAFailedTrayHostSwitchKeepsThePreviousConnection() throws {
        let probe = SwitchProbe { $0 == self.remote }
        let session = AppSessionStatus()
        let view = try connect(remote, probe: probe, session: session)

        view.switchServer(to: .host(try XCTUnwrap(Origin(other))))

        XCTAssertEqual(probe.checked, [remote, other])
        XCTAssertEqual(session.currentOrigin?.canonicalKey, remote, "A refused probe must not half-switch the session")
        XCTAssertEqual(session.isConnected, true)
        XCTAssertEqual(lastServer.savedOrigin?.canonicalKey, remote, "A refused host is never remembered")
        XCTAssertFalse(hostStore.hosts.contains { $0.origin == other })
    }

    func testTheCurrentHostIsNotProbedAgain() throws {
        let probe = SwitchProbe { _ in true }
        let session = AppSessionStatus()
        let view = try connect(remote, probe: probe, session: session)

        view.switchServer(to: .host(try XCTUnwrap(Origin(remote))))

        XCTAssertEqual(probe.checked, [remote], "Choosing the connected row does nothing")
        XCTAssertEqual(session.currentOrigin?.canonicalKey, remote)
    }

    func testLocalToHostAsksOnceAndCancelChangesNothing() throws {
        let probe = SwitchProbe { _ in true }
        let session = AppSessionStatus()
        let modeStore = TransientEngineModeStore()
        modeStore.mode = .local
        var asked = 0
        let view = makeView(
            probe: probe,
            session: session,
            engineMode: .local,
            modeStore: modeStore,
            modeSwitchDialog: { _ in asked += 1; return false }
        )

        view.switchServer(to: .host(try XCTUnwrap(Origin(remote))))

        XCTAssertEqual(asked, 1)
        XCTAssertEqual(probe.checked, [], "Cancel never contacts the server")
        XCTAssertEqual(modeStore.mode, .local, "Cancel keeps the local choice")
        XCTAssertNil(session.currentOrigin)
    }

    func testLocalToHostConfirmedSavesExternalAndConnectsToTheChosenHost() throws {
        let probe = SwitchProbe { _ in true }
        let session = AppSessionStatus()
        let modeStore = TransientEngineModeStore()
        modeStore.mode = .local
        var asked = 0
        let view = makeView(
            probe: probe,
            session: session,
            engineMode: .local,
            modeStore: modeStore,
            modeSwitchDialog: { _ in asked += 1; return true }
        )

        view.switchServer(to: .host(try XCTUnwrap(Origin(remote))))

        XCTAssertEqual(asked, 1, "The confirmation is asked once, not once per probe")
        XCTAssertEqual(modeStore.mode, .external)
        XCTAssertEqual(probe.checked, [remote])
        XCTAssertEqual(session.currentOrigin?.canonicalKey, remote)
    }

    func testThisMacInExternalModeWithoutAnEngineDoesNothing() {
        let modeStore = TransientEngineModeStore()
        modeStore.mode = .external
        let view = makeView(probe: SwitchProbe { _ in true }, session: AppSessionStatus(), modeStore: modeStore)

        view.switchServer(to: .thisMac)

        XCTAssertEqual(modeStore.mode, .external, "No bundled engine is offered, so nothing switches")
    }
}
