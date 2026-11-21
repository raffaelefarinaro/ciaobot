import AppKit
import Combine
import XCTest

@testable import CiaobotMac

/// The pure rules behind the tray's Servers section: which rows exist, which
/// one is checked, and when the section is shown at all.
final class ServerSwitchChoicesTests: XCTestCase {
    private let local = "http://127.0.0.1:8443"
    private let remote = "https://vps.example:8443"

    private func record(_ origin: String, label: String? = nil) -> HostRecord {
        HostRecord(origin: origin, label: label ?? HostRecord.defaultLabel(for: origin))
    }

    func testOneHostWithNoLocalEngineHidesTheSection() {
        let rows = ServerSwitchChoices.rows(
            hosts: [record(remote)],
            connected: Origin(remote),
            localMode: false,
            localAvailable: false
        )
        XCTAssertFalse(ServerSwitchChoices.showsSection(rows))
    }

    func testTwoHostsShowTwoRowsAndCheckOnlyTheConnectedOne() throws {
        let rows = ServerSwitchChoices.rows(
            hosts: [record(local), record(remote)],
            connected: Origin(remote),
            localMode: false,
            localAvailable: false
        )
        XCTAssertTrue(ServerSwitchChoices.showsSection(rows))
        XCTAssertEqual(rows, [
            .host(origin: try XCTUnwrap(Origin(local)), label: "127.0.0.1:8443", checked: false),
            .host(origin: try XCTUnwrap(Origin(remote)), label: "vps.example:8443", checked: true),
        ])
    }

    func testALocalCapableMacAddsThisMacFirstAndChecksItInLocalMode() throws {
        let rows = ServerSwitchChoices.rows(
            hosts: [record(remote)],
            connected: nil,
            localMode: true,
            localAvailable: true
        )
        XCTAssertTrue(ServerSwitchChoices.showsSection(rows))
        XCTAssertEqual(rows, [
            .thisMac(checked: true),
            .host(origin: try XCTUnwrap(Origin(remote)), label: "vps.example:8443", checked: false),
        ], "Local mode checks This Mac and never a saved host, even one with the same address shape")
    }

    func testALocalCapableMacWithNoSavedHostHasOneChoiceAndNoSection() {
        let rows = ServerSwitchChoices.rows(hosts: [], connected: nil, localMode: false, localAvailable: true)
        XCTAssertFalse(ServerSwitchChoices.showsSection(rows), "One choice is not a switch")
    }

    func testAnOriginThatNoLongerParsesIsNotShown() throws {
        let rows = ServerSwitchChoices.rows(
            hosts: [record("https://vps.example:8443/admin"), record(remote)],
            connected: Origin(remote),
            localMode: false,
            localAvailable: true
        )
        XCTAssertEqual(rows.count, 2, "The unparsable origin must not become a row")
        XCTAssertEqual(rows.last, .host(origin: try XCTUnwrap(Origin(remote)), label: "vps.example:8443", checked: true))
    }

    func testAHostileLabelIsSanitizedBeforeItBecomesARowTitle() throws {
        let hostile = "Work\nServer\u{202E}evil\u{200B}"
        let rows = ServerSwitchChoices.rows(
            hosts: [record(remote, label: hostile)],
            connected: nil,
            localMode: false,
            localAvailable: true
        )
        guard case .host(_, let label, _) = try XCTUnwrap(rows.last) else {
            return XCTFail("The saved host row is missing")
        }
        XCTAssertEqual(label, "Work Serverevil", "Line breaks become spaces; bidi and zero-width format characters are removed")
        XCTAssertFalse(label.contains("\n"))
        XCTAssertFalse(label.unicodeScalars.contains { $0.value == 0x202E || $0.value == 0x200B })
    }
}

/// The tray's rows as menu items. Every fixture is injected: no row probes,
/// fetches, or touches the operator's real saved hosts or status item.
@MainActor
final class ServerSwitchMenuTests: XCTestCase {
    private final class FakeHosts: HostListStoring {
        var hosts: [HostRecord]
        init(_ hosts: [HostRecord]) { self.hosts = hosts }
        func add(_ origin: Origin) {}
        func remove(_ origin: Origin) {}
        func rename(_ origin: Origin, label: String) {}
    }

    private final class FakeStatusItem: MenuBarStatusItem {
        var title: String?
        var image: NSImage?
        var toolTip: String?
        var menu: NSMenu?
        var isVisible = false
        func remove() {}
    }

    private final class FakeStatusItemFactory: StatusItemFactory {
        func makeStatusItem(length: CGFloat) -> MenuBarStatusItem { FakeStatusItem() }
    }

    private let local = "http://127.0.0.1:8443"
    private let remote = "https://vps.example:8443"

    private func makeController(
        hosts: [HostRecord],
        connected: String? = nil,
        localMode: Bool = false,
        localAvailable: Bool = false,
        requests: @escaping (ServerSwitchRequest) -> Void = { _ in },
        openWindow: @escaping () -> Void = {}
    ) throws -> MenuBarController {
        let status = AppSessionStatus()
        if let connected {
            status.reportConnected(try XCTUnwrap(Origin(connected)))
        }
        let controller = MenuBarController(
            status: status,
            statusItemFactory: FakeStatusItemFactory(),
            openWindow: openWindow,
            quit: {},
            changeHost: {},
            localEngineMode: localMode,
            hosts: FakeHosts(hosts),
            localEngineAvailable: localAvailable,
            serverSwitch: requests
        )
        controller.start()
        return controller
    }

    private func serverRows(_ controller: MenuBarController) -> [NSMenuItem] {
        (controller.menu?.items ?? []).filter {
            $0.action == #selector(MenuBarController.selectServer(_:))
        }
    }

    private func record(_ origin: String, label: String? = nil) -> HostRecord {
        HostRecord(origin: origin, label: label ?? HostRecord.defaultLabel(for: origin))
    }

    func testTheSectionIsHiddenWithOneHostAndNoLocalEngine() throws {
        let controller = try makeController(hosts: [record(remote)], connected: remote)
        XCTAssertEqual(serverRows(controller).count, 0)
    }

    func testTwoHostsShowTwoRowsWithTheConnectedOneChecked() throws {
        let controller = try makeController(
            hosts: [record(local), record(remote)],
            connected: remote
        )
        let rows = serverRows(controller)
        XCTAssertEqual(rows.map(\.title), ["127.0.0.1:8443", "vps.example:8443"])
        XCTAssertEqual(rows.map(\.state), [.off, .on])
    }

    func testALocalCapableMacShowsThisMacFirstWithItsCheckmark() throws {
        let controller = try makeController(
            hosts: [record(remote)],
            connected: nil,
            localMode: true,
            localAvailable: true
        )
        let rows = serverRows(controller)
        XCTAssertEqual(rows.map(\.title), ["This Mac", "vps.example:8443"])
        XCTAssertEqual(rows.map(\.state), [.on, .off])
    }

    func testClickingAnExternalRowRequestsItsSwitchOnceAndOpensNothingItself() throws {
        var requests: [ServerSwitchRequest] = []
        var opened = 0
        let controller = try makeController(
            hosts: [record(local), record(remote)],
            connected: local,
            requests: { requests.append($0) },
            openWindow: { opened += 1 }
        )
        let row = try XCTUnwrap(serverRows(controller).last)

        controller.selectServer(row)

        XCTAssertEqual(requests, [.host(try XCTUnwrap(Origin(remote)))])
        XCTAssertEqual(opened, 0, "Presenting the window is the request's job, not the row's")
    }

    func testClickingThisMacRequestsThisMac() throws {
        var requests: [ServerSwitchRequest] = []
        let controller = try makeController(
            hosts: [record(remote)],
            connected: remote,
            localAvailable: true,
            requests: { requests.append($0) }
        )
        let row = try XCTUnwrap(serverRows(controller).first)

        controller.selectServer(row)

        XCTAssertEqual(requests, [.thisMac])
    }

    func testOpeningTheMenuNeverRequestsASwitchOrChangesTheRows() throws {
        var requests: [ServerSwitchRequest] = []
        let controller = try makeController(
            hosts: [record(local), record(remote)],
            connected: remote,
            localAvailable: true,
            requests: { requests.append($0) }
        )
        let before = serverRows(controller).map(\.title)

        controller.menuWillOpen(try XCTUnwrap(controller.menu))

        XCTAssertEqual(requests, [])
        XCTAssertEqual(serverRows(controller).map(\.title), before)
    }

    func testAHostileLabelBecomesOneSanitizedRow() throws {
        let controller = try makeController(
            hosts: [record(remote, label: "Work\nServer\u{202E}evil")],
            localAvailable: true
        )
        let titles = serverRows(controller).map(\.title)
        XCTAssertEqual(titles, ["This Mac", "Work Serverevil"])
    }
}
