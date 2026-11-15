import XCTest
import AppKit
import Combine

@testable import CiaobotMac

// MARK: - Doubles

@MainActor
private final class BadgeFakeStatusItem: MenuBarStatusItem {
    var title: String?
    var image: NSImage?
    var toolTip: String?
    var menu: NSMenu?
    var isVisible = false

    func remove() {}
}

@MainActor
private final class BadgeFakeStatusItemFactory: StatusItemFactory {
    private(set) var requestedLengths: [CGFloat] = []
    private(set) var items: [BadgeFakeStatusItem] = []

    func makeStatusItem(length: CGFloat) -> MenuBarStatusItem {
        requestedLengths.append(length)
        let item = BadgeFakeStatusItem()
        items.append(item)
        return item
    }
}

@MainActor
private final class BadgeFakeChatList: ChatListProviding {
    let subject: CurrentValueSubject<ChatListState, Never>

    init(_ state: ChatListState = .disconnected) {
        subject = CurrentValueSubject(state)
    }

    var state: ChatListState { subject.value }
    var statePublisher: AnyPublisher<ChatListState, Never> { subject.eraseToAnyPublisher() }

    func start() {}
    func stop() {}
    func refreshIfNeeded() {}
    func set(_ state: ChatListState) { subject.send(state) }
}

@MainActor
private final class BadgeFakeActivity: ChatActivityProviding {
    let subject: CurrentValueSubject<ChatActivityState, Never>

    init(_ state: ChatActivityState = .inactive) {
        subject = CurrentValueSubject(state)
    }

    var state: ChatActivityState { subject.value }
    var statePublisher: AnyPublisher<ChatActivityState, Never> { subject.eraseToAnyPublisher() }

    func start() {}
    func stop() {}
    func refreshIfNeeded() {}
    func set(_ state: ChatActivityState) { subject.send(state) }
}

@MainActor
final class TrayBadgeTests: XCTestCase {
    private let origin = Origin("http://127.0.0.1:8443")!
    private let otherOrigin = Origin("http://127.0.0.1:9443")!
    private let now = Date(timeIntervalSince1970: 1_800_000_000)

    // MARK: - Pure badge formatting

    func testBadgeUsesOnlyPositiveServerCountsAndNeverInventsANumber() {
        XCTAssertEqual(MenuBarController.attentionBadgeTitle(nil), "")
        XCTAssertEqual(MenuBarController.attentionBadgeTitle(0), "")
        XCTAssertEqual(MenuBarController.attentionBadgeTitle(-4), "")
        XCTAssertEqual(MenuBarController.attentionBadgeTitle(1), "1")
        XCTAssertEqual(MenuBarController.attentionBadgeTitle(20), "20")
        XCTAssertEqual(MenuBarController.attentionBadgeTitle(100), "100")
    }

    // MARK: - Status item badge

    func testBadgeShowsTheServersDeduplicatedCountAndDoesNotAddWorkingChats() throws {
        let status = AppSessionStatus()
        status.reportConnected(origin)
        let snapshot = ChatListSnapshot(
            origin: origin.canonicalKey,
            chats: [
                chat("needs", title: "Needs", needsInput: true),
                chat("unread", title: "Unread", unread: true),
                // The same chat appears only once even though both flags hold.
                chat("both", title: "Both", unread: true, needsInput: true),
                chat("working", title: "Working"),
            ],
            attentionCount: 2,
            fetchedAt: now
        )
        let activity = BadgeFakeActivity(
            .live(origin: origin.canonicalKey, activeChatIDs: ["working", "needs", "both"])
        )
        let factory = BadgeFakeStatusItemFactory()
        let controller = MenuBarController(
            status: status,
            statusItemFactory: factory,
            image: NSImage(size: NSSize(width: 40, height: 40)),
            openWindow: {},
            quit: {},
            chatList: BadgeFakeChatList(.loaded(snapshot)),
            openChat: { _ in true },
            activity: activity,
            now: { self.now }
        )
        controller.start()

        XCTAssertEqual(controller.statusItem?.title, "2", "The face keeps the server's deduplicated attention count beside it.")
        XCTAssertEqual(factory.requestedLengths, [NSStatusItem.variableLength], "A count beside the face needs the variable-width status item.")
        let menu = try XCTUnwrap(controller.menu)
        XCTAssertFalse(
            menu.items.contains { $0.title == "2 chats need attention" },
            "The badge replaces the duplicate in-menu count."
        )
        XCTAssertNotNil(menu.items.first { $0.title == "Needs · now" })
        XCTAssertNotNil(menu.items.first { $0.title == "Working · now" })
    }

    func testBadgeClearsOnZeroUnknownAndDisconnectBeforeTheDeferredRebuild() {
        let status = AppSessionStatus()
        status.reportConnected(origin)
        let chatList = BadgeFakeChatList(.loaded(snapshot(count: 2)))
        let controller = MenuBarController(
            status: status,
            statusItemFactory: BadgeFakeStatusItemFactory(),
            image: NSImage(size: NSSize(width: 40, height: 40)),
            openWindow: {},
            quit: {},
            chatList: chatList,
            openChat: { _ in true },
            now: { self.now }
        )
        controller.start()
        XCTAssertEqual(controller.statusItem?.title, "2")

        chatList.set(.loaded(snapshot(count: 0)))
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        XCTAssertEqual(controller.statusItem?.title, "", "Zero attention must not leave a stale count.")

        chatList.set(.loaded(snapshot(count: 4)))
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        XCTAssertEqual(controller.statusItem?.title, "4")

        chatList.set(.loaded(snapshot(count: nil)))
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        XCTAssertEqual(controller.statusItem?.title, "", "An unknown count must not be shown as a number.")

        chatList.set(.loaded(snapshot(count: 3)))
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        XCTAssertEqual(controller.statusItem?.title, "3")

        status.reportDisconnected()
        XCTAssertEqual(
            controller.statusItem?.title,
            "",
            "A disconnect must clear the badge immediately, not after the deferred menu rebuild."
        )
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        XCTAssertEqual(controller.statusItem?.title, "")
    }

    func testBadgeRejectsAnotherOriginsSnapshotAndLateAsyncUpdates() {
        let status = AppSessionStatus()
        status.reportConnected(origin)
        let chatList = BadgeFakeChatList(.loaded(snapshot(count: 7, origin: otherOrigin)))
        let controller = MenuBarController(
            status: status,
            statusItemFactory: BadgeFakeStatusItemFactory(),
            image: NSImage(size: NSSize(width: 40, height: 40)),
            openWindow: {},
            quit: {},
            chatList: chatList,
            openChat: { _ in true },
            now: { self.now }
        )
        controller.start()
        XCTAssertEqual(controller.statusItem?.title, "", "Another origin's count is never shown.")

        chatList.set(.loaded(snapshot(count: 4, origin: origin)))
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        XCTAssertEqual(controller.statusItem?.title, "4")

        status.reportConnected(otherOrigin)
        XCTAssertEqual(
            controller.statusItem?.title,
            "",
            "Switching origin clears the old badge before the deferred rebuild."
        )
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        XCTAssertEqual(controller.statusItem?.title, "", "The previous origin's loaded snapshot is not adopted.")

        // A response for the old origin that lands after the switch is dropped.
        chatList.set(.loaded(snapshot(count: 8, origin: origin)))
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        XCTAssertEqual(controller.statusItem?.title, "")

        chatList.set(.loaded(snapshot(count: 6, origin: otherOrigin)))
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        XCTAssertEqual(controller.statusItem?.title, "6")

        // A snapshot that is already queued when the connection drops must not
        // paint a number after the disconnect.
        chatList.set(.loaded(snapshot(count: 9, origin: otherOrigin)))
        status.reportDisconnected()
        XCTAssertEqual(controller.statusItem?.title, "")
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        XCTAssertEqual(controller.statusItem?.title, "")
    }

    func testBadgeLeavesTheFallbackMarkVisibleWhenArtworkIsMissing() {
        let status = AppSessionStatus()
        status.reportConnected(origin)
        let chatList = BadgeFakeChatList(.loaded(snapshot(count: 2)))
        let controller = MenuBarController(
            status: status,
            statusItemFactory: BadgeFakeStatusItemFactory(),
            image: nil,
            openWindow: {},
            quit: {},
            chatList: chatList,
            openChat: { _ in true },
            now: { self.now }
        )
        controller.start()

        XCTAssertEqual(controller.statusItem?.title, "C 2", "The fallback mark and the count both stay visible.")
        chatList.set(.loaded(snapshot(count: 0)))
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        XCTAssertEqual(controller.statusItem?.title, MenuBarController.fallbackTitle)
    }

    func testBadgeClearsWhenTheFeedIsLoadingUnavailableOrTheCountIsAbsent() {
        let status = AppSessionStatus()
        status.reportConnected(origin)
        let chatList = BadgeFakeChatList(.loaded(snapshot(count: 5)))
        let controller = MenuBarController(
            status: status,
            statusItemFactory: BadgeFakeStatusItemFactory(),
            image: NSImage(size: NSSize(width: 40, height: 40)),
            openWindow: {},
            quit: {},
            chatList: chatList,
            openChat: { _ in true },
            now: { self.now }
        )
        controller.start()
        XCTAssertEqual(controller.statusItem?.title, "5")

        for state in [ChatListState.loading, .unavailable(.notSignedIn), .disconnected] {
            chatList.set(state)
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
            XCTAssertEqual(controller.statusItem?.title, "", "No badge for \(state)")
        }
    }

    // MARK: - Helpers

    private func snapshot(count: Int?, origin: Origin? = nil) -> ChatListSnapshot {
        ChatListSnapshot(
            origin: (origin ?? self.origin).canonicalKey,
            chats: [chat("chat-1", title: "Chat", unread: true)],
            attentionCount: count,
            fetchedAt: now
        )
    }

    private func chat(
        _ id: String,
        title: String? = nil,
        unread: Bool = false,
        needsInput: Bool = false
    ) -> MenubarChat {
        MenubarChat(
            chatID: id,
            title: title ?? id,
            workspace: "work",
            lastActivityAt: iso(now),
            unread: unread,
            needsInput: needsInput
        )
    }

    private func iso(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.string(from: date)
    }
}
