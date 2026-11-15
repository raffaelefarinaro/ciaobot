import XCTest
import AppKit
import Combine
import WebKit
import UserNotifications

@testable import CiaobotMac

// MARK: - Doubles

private final class TrayFakeCookieProvider: ChatCookieProviding {
    var cookies: [HTTPCookie] = []
    private(set) var callCount = 0

    func cookies(for url: URL, completion: @escaping ([HTTPCookie]) -> Void) {
        callCount += 1
        completion(cookies)
    }
}

private final class TrayFakeConnection: EventStreamConnection {
    private let onEvent: (ChatEvent) -> Void
    private let onClose: (EventStreamCloseReason) -> Void
    private(set) var cancelCount = 0

    init(onEvent: @escaping (ChatEvent) -> Void, onClose: @escaping (EventStreamCloseReason) -> Void) {
        self.onEvent = onEvent
        self.onClose = onClose
    }

    func emit(_ event: ChatEvent) { onEvent(event) }
    func close(_ reason: EventStreamCloseReason) { onClose(reason) }
    func cancel() { cancelCount += 1 }
}

private final class TrayFakeEventTransport: EventStreamTransport {
    struct Open {
        let origin: Origin
        let cookieHeader: String
        let connection: TrayFakeConnection
    }

    private(set) var opens: [Open] = []
    var failOpen = false

    func open(
        origin: Origin,
        cookieHeader: String,
        onEvent: @escaping (ChatEvent) -> Void,
        onClose: @escaping (EventStreamCloseReason) -> Void
    ) -> EventStreamConnection? {
        guard !failOpen else { return nil }
        let connection = TrayFakeConnection(onEvent: onEvent, onClose: onClose)
        opens.append(Open(origin: origin, cookieHeader: cookieHeader, connection: connection))
        return connection
    }
}

@MainActor
private final class TrayFakeActivity: ChatActivityProviding {
    let subject: CurrentValueSubject<ChatActivityState, Never>
    private(set) var startCount = 0
    private(set) var stopCount = 0
    private(set) var refreshCount = 0

    init(_ state: ChatActivityState = .inactive) {
        subject = CurrentValueSubject(state)
    }

    var state: ChatActivityState { subject.value }
    var statePublisher: AnyPublisher<ChatActivityState, Never> { subject.eraseToAnyPublisher() }

    func start() { startCount += 1 }
    func stop() { stopCount += 1 }
    func refreshIfNeeded() { refreshCount += 1 }
    func set(_ state: ChatActivityState) { subject.send(state) }
}

@MainActor
private final class TrayFakeChatList: ChatListProviding {
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
private final class TrayFakeStatusItem: MenuBarStatusItem {
    var title: String?
    var image: NSImage?
    var toolTip: String?
    var menu: NSMenu?
    var isVisible = false

    func remove() {}
}

@MainActor
private final class TrayFakeStatusItemFactory: StatusItemFactory {
    func makeStatusItem(length: CGFloat) -> MenuBarStatusItem {
        TrayFakeStatusItem()
    }
}

@MainActor
private final class TrayFakeNotificationMenu: NotificationMenuProviding {
    let subject: CurrentValueSubject<ChatNotificationState, Never>
    let streamSubject: CurrentValueSubject<ChatNotificationStreamStatus, Never>
    private(set) var actionCount = 0

    init(_ state: ChatNotificationState = .off, stream: ChatNotificationStreamStatus = .ready) {
        subject = CurrentValueSubject(state)
        streamSubject = CurrentValueSubject(stream)
    }

    var state: ChatNotificationState { subject.value }
    var statePublisher: AnyPublisher<ChatNotificationState, Never> { subject.eraseToAnyPublisher() }
    var streamStatus: ChatNotificationStreamStatus { streamSubject.value }
    var streamStatusPublisher: AnyPublisher<ChatNotificationStreamStatus, Never> {
        streamSubject.eraseToAnyPublisher()
    }

    func performMenuAction() { actionCount += 1 }
    func refreshIfNeeded() {}
    func stop() {}
}

@MainActor
private final class TrayFakeNotificationPermission: NotificationPermissionClient {
    var isAvailable = true
    var onActivation: ((ChatNotificationTarget) -> Void)?
    var authorization: ChatNotificationAuthorization = .notDetermined

    private(set) var prepareCount = 0
    private(set) var statusCount = 0
    private(set) var requestCount = 0

    func prepare() { prepareCount += 1 }

    func authorizationStatus(completion: @escaping (ChatNotificationAuthorization) -> Void) {
        statusCount += 1
        completion(authorization)
    }

    func requestAuthorization(completion: @escaping (ChatNotificationAuthorizationRequest) -> Void) {
        requestCount += 1
        authorization = .authorized
        completion(.granted)
    }

    func deliver(_ message: ChatNotificationMessage) {}
    func removeDelivered(identifier: String) {}
}

private final class TrayMemoryNotificationPreference: NotificationPreferenceStoring {
    var isEnabled = false
}

// MARK: - Tests

@MainActor
final class TrayAttentionTests: XCTestCase {
    private let origin = Origin("http://127.0.0.1:8443")!
    private let otherOrigin = Origin("http://127.0.0.1:9443")!
    private let now = Date(timeIntervalSince1970: 1_800_000_000)

    // MARK: Filtering and precedence

    func testSectionsKeepOnlyNeedsYouWorkingAndUnreadWithHomepagePrecedence() {
        let snapshot = ChatListSnapshot(
            origin: origin.canonicalKey,
            chats: [
                chat("quiet", title: "Quiet"),
                chat("unread", title: "Unread", unread: true),
                chat("needs", title: "Needs", needsInput: true),
                chat("working", title: "Working"),
                chat("needs-unread-working", title: "Both", unread: true, needsInput: true),
                chat("unread-working", title: "Reading", unread: true),
                chat("working-only", title: "Running"),
            ],
            attentionCount: 4,
            fetchedAt: now
        )
        let activity = ChatActivityState.live(
            origin: origin.canonicalKey,
            activeChatIDs: ["working", "needs-unread-working", "unread-working", "working-only"]
        )

        let sections = TrayChatList.sections(snapshot: snapshot, activity: activity, now: now)

        XCTAssertEqual(sections.map(\.tier), [.needsYou, .working, .unread])
        XCTAssertEqual(sections[0].rows.map(\.chat.chatID), ["needs", "needs-unread-working"])
        XCTAssertEqual(sections[1].rows.map(\.chat.chatID), ["working", "working-only"])
        XCTAssertEqual(sections[2].rows.map(\.chat.chatID), ["unread", "unread-working"])
        XCTAssertEqual(sections.flatMap(\.rows).count, 6, "Every chat must appear once")
        XCTAssertFalse(sections.flatMap(\.rows).contains { $0.chat.chatID == "quiet" })
        XCTAssertTrue(sections[2].rows[1].working, "The row keeps its secondary working fact")
    }

    func testWorkingStateIsNotInferredWithoutALiveFeedForThisOrigin() {
        let snapshot = ChatListSnapshot(
            origin: origin.canonicalKey,
            chats: [chat("working", title: "Running")],
            attentionCount: 0,
            fetchedAt: now
        )
        let states: [ChatActivityState] = [
            .inactive,
            .connecting(origin: origin.canonicalKey),
            .unavailable(origin: origin.canonicalKey, failure: .unavailable),
            .live(origin: otherOrigin.canonicalKey, activeChatIDs: ["working"]),
        ]
        for state in states {
            XCTAssertTrue(
                TrayChatList.sections(snapshot: snapshot, activity: state, now: now).isEmpty,
                "Working must not be invented from \(state)"
            )
        }

        XCTAssertEqual(
            TrayChatList.sections(
                snapshot: snapshot,
                activity: .live(origin: origin.canonicalKey, activeChatIDs: ["working"]),
                now: now
            ).first?.tier,
            .working
        )
    }

    func testDuplicateRowsAreCollapsedAndFirstKeepsItsState() {
        let snapshot = ChatListSnapshot(
            origin: origin.canonicalKey,
            chats: [
                chat("same", title: "First", unread: true),
                chat("same", title: "Second", needsInput: true),
            ],
            attentionCount: 1,
            fetchedAt: now
        )
        let rows = TrayChatList.sections(snapshot: snapshot, activity: nil, now: now).flatMap(\.rows)
        XCTAssertEqual(rows.count, 1)
        XCTAssertEqual(rows.first?.chat.title, "First")
        XCTAssertEqual(rows.first?.tier, .unread)
    }

    // MARK: Relative time

    func testWorkspaceGroupsFollowMostUrgentRowAndKeepTierOrder() {
        let snapshot = ChatListSnapshot(
            origin: origin.canonicalKey,
            chats: [
                chat("a", workspace: "work", unread: true, lastActivityAt: iso(now.addingTimeInterval(0))),
                chat("b", workspace: "personal", needsInput: true, lastActivityAt: iso(now.addingTimeInterval(-60))),
                chat("c", workspace: "work", needsInput: true, lastActivityAt: iso(now.addingTimeInterval(-120))),
                chat("d", workspace: "personal", unread: true, lastActivityAt: iso(now.addingTimeInterval(-180))),
            ],
            attentionCount: 4,
            fetchedAt: now
        )
        let sections = TrayChatList.sections(snapshot: snapshot, activity: nil, now: now)

        let groups = TrayChatList.workspaceGroups(sections)

        XCTAssertEqual(groups.map(\.workspace), ["personal", "work"])
        XCTAssertEqual(groups.map { $0.rows.map(\.chat.chatID) }, [["b", "d"], ["c", "a"]])
        XCTAssertEqual(groups[0].rows.map(\.tier), [.needsYou, .unread])
    }

    func testWorkspaceGroupsMergeLabelsThatOnlyDifferByControlCharacters() {
        let snapshot = ChatListSnapshot(
            origin: origin.canonicalKey,
            chats: [
                chat("one", workspace: "work", unread: true),
                chat("two", workspace: "work\u{202E}\n", unread: true),
            ],
            attentionCount: 2,
            fetchedAt: now
        )
        let sections = TrayChatList.sections(snapshot: snapshot, activity: nil, now: now)

        let groups = TrayChatList.workspaceGroups(sections)

        XCTAssertEqual(groups.map(\.workspace), ["work"])
        XCTAssertEqual(groups[0].rows.map(\.chat.chatID), ["one", "two"])
    }

    func testRelativeTimeUsesTheHomepageUnits() {
        let formatter = ISO8601DateFormatter()
        func iso(_ secondsAgo: TimeInterval) -> String {
            formatter.string(from: now.addingTimeInterval(-secondsAgo))
        }

        let cases: [(TimeInterval, String)] = [
            (0, "now"),
            (59, "now"),
            (60, "1m"),
            (3_599, "59m"),
            (3_600, "1h"),
            (86_399, "23h"),
            (86_400, "1d"),
            (6 * 86_400, "6d"),
            (7 * 86_400, "1w"),
            (27 * 86_400, "3w"),
            (28 * 86_400, "1mo"),
            (60 * 86_400, "2mo"),
        ]
        for (seconds, expected) in cases {
            XCTAssertEqual(
                TrayRelativeTime.label(from: iso(seconds), now: now),
                expected,
                "\(seconds)s should read \(expected)"
            )
        }

        XCTAssertEqual(
            TrayRelativeTime.label(from: formatter.string(from: now.addingTimeInterval(360)), now: now),
            "now",
            "A clock-skewed future timestamp must not show a negative unit"
        )
    }

    func testRelativeTimeAcceptsFractionalSecondsAndRejectsAbsentOrInvalidValues() {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let fractional = formatter.string(from: now.addingTimeInterval(-125))
        XCTAssertEqual(TrayRelativeTime.label(from: fractional, now: now), "2m")

        for invalid in ["", "   ", "not a date", "2026-13-40T99:99:99Z"] {
            XCTAssertNil(TrayRelativeTime.label(from: invalid, now: now), "No unit for \(invalid)")
        }
    }

    // MARK: Activity model

    func testActivityModelReadsSnapshotAndStreamingEvents() {
        let status = AppSessionStatus()
        let transport = TrayFakeEventTransport()
        let provider = TrayFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let model = ChatActivityModel(
            context: status,
            transport: transport,
            cookieProviderFactory: { _ in provider },
            now: { self.now }
        )
        model.start()
        status.reportConnected(origin, dataStore: .nonPersistent())

        XCTAssertTrue(waitUntil { transport.opens.count == 1 })
        XCTAssertEqual(transport.opens[0].origin.canonicalKey, origin.canonicalKey)
        XCTAssertEqual(transport.opens[0].cookieHeader, "ciao_session=secret")
        XCTAssertEqual(model.state, .connecting(origin: origin.canonicalKey))

        let connection = transport.opens[0].connection
        connection.emit(.snapshot(activeChatIDs: ["a"]))
        XCTAssertEqual(
            model.state,
            .live(origin: origin.canonicalKey, activeChatIDs: ["a"])
        )

        connection.emit(.streamingStarted(chatID: "b"))
        XCTAssertEqual(
            model.state,
            .live(origin: origin.canonicalKey, activeChatIDs: ["a", "b"])
        )

        connection.emit(.streamingDone(chatID: "a"))
        XCTAssertEqual(
            model.state,
            .live(origin: origin.canonicalKey, activeChatIDs: ["b"])
        )

        connection.emit(.resultReady(chatID: "b", title: "Done", snippet: "Ready", workspace: ""))
        XCTAssertEqual(
            model.state,
            .live(origin: origin.canonicalKey, activeChatIDs: [])
        )
        model.stop()
        XCTAssertEqual(connection.cancelCount, 1, "Stopping the tray must close its own socket")
    }

    func testActivityModelIgnoresEventsBeforeTheSnapshotAndClearsOnClose() {
        let status = AppSessionStatus()
        let transport = TrayFakeEventTransport()
        let provider = TrayFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let model = ChatActivityModel(
            context: status,
            transport: transport,
            cookieProviderFactory: { _ in provider }
        )
        model.start()
        status.reportConnected(origin, dataStore: .nonPersistent())
        XCTAssertTrue(waitUntil { transport.opens.count == 1 })

        let first = transport.opens[0].connection
        first.emit(.streamingStarted(chatID: "before-snapshot"))
        XCTAssertEqual(model.state, .connecting(origin: origin.canonicalKey))

        first.emit(.snapshot(activeChatIDs: ["a"]))
        XCTAssertEqual(model.state, .live(origin: origin.canonicalKey, activeChatIDs: ["a"]))

        first.close(.ended)
        XCTAssertEqual(
            model.state,
            .unavailable(origin: origin.canonicalKey, failure: .unavailable)
        )
        XCTAssertNil(model.state.workingChatIDs(for: origin.canonicalKey))

        // A late event from the closed connection must not resurrect the row.
        first.emit(.snapshot(activeChatIDs: ["a"]))
        XCTAssertEqual(
            model.state,
            .unavailable(origin: origin.canonicalKey, failure: .unavailable)
        )

        model.refreshIfNeeded()
        XCTAssertTrue(waitUntil { transport.opens.count == 2 })
        transport.opens[1].connection.emit(.snapshot(activeChatIDs: ["b"]))
        XCTAssertEqual(model.state, .live(origin: origin.canonicalKey, activeChatIDs: ["b"]))
        transport.opens[1].connection.close(.unauthorized)
        XCTAssertEqual(
            model.state,
            .unavailable(origin: origin.canonicalKey, failure: .notSignedIn)
        )
        model.stop()
    }

    func testActivityModelNeverSendsARemoteHandshakeWithoutTheExactSessionCookie() {
        let status = AppSessionStatus()
        let transport = TrayFakeEventTransport()
        let provider = TrayFakeCookieProvider()
        provider.cookies = [makeCookie(domain: "other.example")]
        let model = ChatActivityModel(
            context: status,
            transport: transport,
            cookieProviderFactory: { _ in provider }
        )
        let remote = Origin("https://example.com")!
        model.start()
        status.reportConnected(remote, dataStore: .nonPersistent())

        XCTAssertTrue(waitUntil {
            model.state == .unavailable(origin: remote.canonicalKey, failure: .notSignedIn)
        })
        XCTAssertTrue(transport.opens.isEmpty, "No unauthenticated /ws/events handshake")

        provider.cookies = [
            makeCookie(name: "other", value: "keep", domain: "example.com", secure: true),
            makeCookie(domain: "example.com", secure: true),
        ]
        model.refreshIfNeeded()
        XCTAssertTrue(waitUntil { transport.opens.count == 1 })
        XCTAssertEqual(transport.opens[0].origin.canonicalKey, remote.canonicalKey)
        XCTAssertEqual(transport.opens[0].cookieHeader, "ciao_session=secret")
        model.stop()
    }

    func testActivityModelDropsThePreviousOriginsSocketAndEvents() {
        let status = AppSessionStatus()
        let transport = TrayFakeEventTransport()
        let provider = TrayFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let model = ChatActivityModel(
            context: status,
            transport: transport,
            cookieProviderFactory: { _ in provider }
        )
        model.start()
        status.reportConnected(origin, dataStore: .nonPersistent())
        XCTAssertTrue(waitUntil { transport.opens.count == 1 })
        let first = transport.opens[0].connection
        first.emit(.snapshot(activeChatIDs: ["old"]))

        status.reportConnected(otherOrigin, dataStore: .nonPersistent())
        XCTAssertTrue(waitUntil { transport.opens.count == 2 })
        XCTAssertEqual(transport.opens[1].origin.canonicalKey, otherOrigin.canonicalKey)
        XCTAssertGreaterThanOrEqual(first.cancelCount, 1)

        // A late event from the replaced origin must not become current state.
        first.emit(.snapshot(activeChatIDs: ["old"]))
        XCTAssertEqual(model.state, .connecting(origin: otherOrigin.canonicalKey))

        transport.opens[1].connection.emit(.snapshot(activeChatIDs: ["new"]))
        XCTAssertEqual(model.state, .live(origin: otherOrigin.canonicalKey, activeChatIDs: ["new"]))
        model.stop()
    }

    func testActivityModelWorksWhileNotificationsAreOffAndNeverPrompts() {
        let status = AppSessionStatus()
        let transport = TrayFakeEventTransport()
        let provider = TrayFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let permission = TrayFakeNotificationPermission()
        let preferences = TrayMemoryNotificationPreference()
        let notifications = ChatNotificationController(
            status: status,
            permission: permission,
            preferences: preferences,
            transport: transport,
            cookieProviderFactory: { _ in provider },
            presentWindow: {},
            openChat: { _ in true }
        )
        let activity = ChatActivityModel(
            context: status,
            transport: transport,
            cookieProviderFactory: { _ in provider }
        )
        notifications.start()
        activity.start()
        status.reportConnected(origin, dataStore: .nonPersistent())

        XCTAssertTrue(waitUntil { transport.opens.count == 1 }, "The working feed must open with notifications off")
        XCTAssertEqual(permission.requestCount, 0, "The tray must never ask for notification permission")
        XCTAssertEqual(permission.prepareCount, 1, "Preparing the delegate is not a prompt")
        XCTAssertEqual(notifications.state, .off)

        transport.opens[0].connection.emit(.snapshot(activeChatIDs: ["a"]))
        XCTAssertEqual(activity.state, .live(origin: origin.canonicalKey, activeChatIDs: ["a"]))
        activity.stop()
        notifications.stop()
    }

    // MARK: Menu presentation

    func testMenuShowsWorkingRowsWithNotificationsOffAndNoPrompt() throws {
        let status = AppSessionStatus()
        status.reportConnected(origin)
        let snapshot = ChatListSnapshot(
            origin: origin.canonicalKey,
            chats: [chat("chat-1", title: "Running", lastActivityAt: iso(now.addingTimeInterval(-120)))],
            attentionCount: 0,
            fetchedAt: now
        )
        let activity = TrayFakeActivity(.live(origin: origin.canonicalKey, activeChatIDs: ["chat-1"]))
        let notifications = TrayFakeNotificationMenu(.off)
        let controller = MenuBarController(
            status: status,
            statusItemFactory: TrayFakeStatusItemFactory(),
            image: nil,
            openWindow: {},
            quit: {},
            chatList: TrayFakeChatList(.loaded(snapshot)),
            openChat: { _ in true },
            notifications: notifications,
            activity: activity,
            now: { self.now }
        )
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        let row = try XCTUnwrap(menu.item(withTitle: "Running · 2m"))
        XCTAssertTrue(row.isEnabled)
        XCTAssertNotNil(row.image)
        XCTAssertEqual(row.image?.isTemplate, true)
        XCTAssertNil(menu.item(withTitle: "working"), "The glyph and the row carry the tier; a header would repeat them.")
        XCTAssertNil(menu.item(withTitle: MenuBarController.noAttentionTitle), "A working row is not 'no attention'.")
        XCTAssertEqual(
            controller.statusItem?.title,
            MenuBarController.fallbackTitle,
            "Working chats do not become an attention number; this controller has no artwork."
        )
        XCTAssertEqual(notifications.actionCount, 0, "Rendering a working row must not prompt")
        XCTAssertEqual(controller.statusItem?.toolTip, "Ciaobot — 1 working")

        activity.set(.live(origin: origin.canonicalKey, activeChatIDs: []))
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        XCTAssertNil(controller.menu?.item(withTitle: "Running · 2m"))
        XCTAssertNotNil(controller.menu?.item(withTitle: MenuBarController.noAttentionTitle))
        XCTAssertEqual(controller.statusItem?.toolTip, "Ciaobot")
    }

    func testMenuDoesNotInventWorkingOrPromiseAttentionWhenTheFeedIsUnavailable() throws {
        let status = AppSessionStatus()
        status.reportConnected(origin)
        let snapshot = ChatListSnapshot(
            origin: origin.canonicalKey,
            chats: [
                chat("unread", title: "Read me", unread: true),
                chat("working", title: "Unknown state"),
            ],
            attentionCount: 1,
            fetchedAt: now
        )
        let activity = TrayFakeActivity(.unavailable(origin: origin.canonicalKey, failure: .notSignedIn))
        let controller = MenuBarController(
            status: status,
            statusItemFactory: TrayFakeStatusItemFactory(),
            image: nil,
            openWindow: {},
            quit: {},
            chatList: TrayFakeChatList(.loaded(snapshot)),
            openChat: { _ in true },
            activity: activity,
            now: { self.now }
        )
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        XCTAssertNotNil(menu.item(withTitle: "Read me · now"))
        XCTAssertNil(menu.item(withTitle: "Unknown state · now"))
        XCTAssertNil(menu.item(withTitle: "working"))
        XCTAssertNotNil(menu.item(withTitle: "Working status unavailable — sign in"))
        XCTAssertNil(menu.item(withTitle: MenuBarController.noAttentionTitle))

        let empty = ChatListSnapshot(
            origin: origin.canonicalKey,
            chats: [chat("quiet", title: "Quiet")],
            attentionCount: 0,
            fetchedAt: now
        )
        let emptyController = MenuBarController(
            status: status,
            statusItemFactory: TrayFakeStatusItemFactory(),
            image: nil,
            openWindow: {},
            quit: {},
            chatList: TrayFakeChatList(.loaded(empty)),
            openChat: { _ in true },
            activity: TrayFakeActivity(.unavailable(origin: origin.canonicalKey, failure: .unavailable)),
            now: { self.now }
        )
        emptyController.start()
        let emptyMenu = try XCTUnwrap(emptyController.menu)
        XCTAssertNotNil(emptyMenu.item(withTitle: MenuBarController.noRelevantChatsTitle))
        XCTAssertNil(emptyMenu.item(withTitle: MenuBarController.noAttentionTitle))
        XCTAssertNotNil(emptyMenu.item(withTitle: "Working status unavailable"))
    }

    func testEmptyVisibleRowsNeverContradictTheServersAttentionCount() throws {
        let status = AppSessionStatus()
        status.reportConnected(origin)
        let snapshot = ChatListSnapshot(
            origin: origin.canonicalKey,
            chats: [chat("quiet", title: "Quiet")],
            attentionCount: 3,
            fetchedAt: now
        )
        let controller = MenuBarController(
            status: status,
            statusItemFactory: TrayFakeStatusItemFactory(),
            image: nil,
            openWindow: {},
            quit: {},
            chatList: TrayFakeChatList(.loaded(snapshot)),
            openChat: { _ in true },
            activity: TrayFakeActivity(.live(origin: origin.canonicalKey, activeChatIDs: [])),
            now: { self.now }
        )
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        XCTAssertNotNil(menu.item(withTitle: MenuBarController.attentionTitle(3)))
        XCTAssertNil(
            menu.item(withTitle: MenuBarController.noAttentionTitle),
            "The visible page has no flagged row, but the server still reports three."
        )
        XCTAssertNil(menu.item(withTitle: MenuBarController.noRelevantChatsTitle))
        XCTAssertEqual(
            controller.statusItem?.title,
            "\(MenuBarController.fallbackTitle) 3",
            "The badge remains the endpoint's attention count."
        )
    }

    func testMenuNeverUsesAnotherOriginsActivity() throws {
        let status = AppSessionStatus()
        status.reportConnected(origin)
        let snapshot = ChatListSnapshot(
            origin: origin.canonicalKey,
            chats: [chat("chat-1", title: "Same id elsewhere")],
            attentionCount: 0,
            fetchedAt: now
        )
        let activity = TrayFakeActivity(
            .live(origin: otherOrigin.canonicalKey, activeChatIDs: ["chat-1"])
        )
        let controller = MenuBarController(
            status: status,
            statusItemFactory: TrayFakeStatusItemFactory(),
            image: nil,
            openWindow: {},
            quit: {},
            chatList: TrayFakeChatList(.loaded(snapshot)),
            openChat: { _ in true },
            activity: activity,
            now: { self.now }
        )
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        XCTAssertNil(menu.item(withTitle: "Same id elsewhere · now"))
        XCTAssertNil(menu.item(withTitle: "working"))
        XCTAssertNil(menu.item(withTitle: "Working status connecting…"))
        XCTAssertNotNil(menu.item(withTitle: MenuBarController.noRelevantChatsTitle))
        XCTAssertEqual(controller.statusItem?.toolTip, "Ciaobot")
    }

    func testMenuTierGlyphsMatchTheHomepageMeanings() throws {
        let status = AppSessionStatus()
        status.reportConnected(origin)
        let snapshot = ChatListSnapshot(
            origin: origin.canonicalKey,
            chats: [
                chat("needs", title: "Needs", needsInput: true),
                chat("working", title: "Working"),
                chat("unread", title: "Unread", unread: true),
            ],
            attentionCount: 2,
            fetchedAt: now
        )
        let controller = MenuBarController(
            status: status,
            statusItemFactory: TrayFakeStatusItemFactory(),
            image: nil,
            openWindow: {},
            quit: {},
            chatList: TrayFakeChatList(.loaded(snapshot)),
            openChat: { _ in true },
            activity: TrayFakeActivity(
                .live(origin: origin.canonicalKey, activeChatIDs: ["working"])
            ),
            now: { self.now }
        )
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        for (title, description, phrase) in [
            ("Needs · now", "Needs your answer", "waiting for you"),
            ("Working · now", "Working", "agent is working"),
            ("Unread · now", "Unread chat", "new reply"),
        ] {
            let row = try XCTUnwrap(menu.item(withTitle: title))
            XCTAssertEqual(row.image?.isTemplate, true)
            XCTAssertEqual(row.image?.accessibilityDescription, description)
            XCTAssertEqual(row.toolTip?.hasPrefix(phrase), true)
        }
    }

    func testMenuRowsOmitTimeWhenTheServerTimestampIsAbsentOrInvalid() throws {
        let status = AppSessionStatus()
        status.reportConnected(origin)
        let snapshot = ChatListSnapshot(
            origin: origin.canonicalKey,
            chats: [
                chat("missing", title: "Missing", unread: true, lastActivityAt: ""),
                chat("invalid", title: "Invalid", unread: true, lastActivityAt: "not-a-date"),
            ],
            attentionCount: 2,
            fetchedAt: now
        )
        let controller = MenuBarController(
            status: status,
            statusItemFactory: TrayFakeStatusItemFactory(),
            image: nil,
            openWindow: {},
            quit: {},
            chatList: TrayFakeChatList(.loaded(snapshot)),
            openChat: { _ in true },
            now: { self.now }
        )
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        XCTAssertNotNil(menu.item(withTitle: "Missing"))
        XCTAssertNotNil(menu.item(withTitle: "Invalid"))
        XCTAssertNil(menu.item(withTitle: "Missing · now"))
        XCTAssertNil(menu.item(withTitle: "Invalid · now"))
    }

    func testMenuRefreshesAndStopsTheActivityFeedWithItsOwnLifecycle() {
        let status = AppSessionStatus()
        status.reportConnected(origin)
        let activity = TrayFakeActivity(.inactive)
        let controller = MenuBarController(
            status: status,
            statusItemFactory: TrayFakeStatusItemFactory(),
            image: nil,
            openWindow: {},
            quit: {},
            chatList: TrayFakeChatList(.loaded(ChatListSnapshot(
                origin: origin.canonicalKey,
                chats: [chat("c1", title: "Chat", unread: true)],
                attentionCount: 1,
                fetchedAt: now
            ))),
            openChat: { _ in true },
            activity: activity
        )
        controller.start()
        XCTAssertEqual(activity.startCount, 1)
        let menu = controller.menu!
        controller.menuWillOpen(menu)
        XCTAssertEqual(activity.refreshCount, 1)
        controller.stop()
        XCTAssertEqual(activity.stopCount, 1)
    }

    // MARK: Helpers

    private func chat(
        _ id: String,
        title: String? = nil,
        workspace: String = "work",
        unread: Bool = false,
        needsInput: Bool = false,
        lastActivityAt: String? = nil
    ) -> MenubarChat {
        MenubarChat(
            chatID: id,
            title: title ?? id,
            workspace: workspace,
            lastActivityAt: lastActivityAt ?? iso(now),
            unread: unread,
            needsInput: needsInput
        )
    }

    private func iso(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.string(from: date)
    }

    private func makeCookie(
        name: String = "ciao_session",
        value: String = "secret",
        domain: String = "127.0.0.1",
        path: String = "/",
        secure: Bool = false
    ) -> HTTPCookie {
        var properties: [HTTPCookiePropertyKey: Any] = [
            .name: name,
            .value: value,
            .domain: domain,
            .path: path,
        ]
        if secure { properties[.secure] = "TRUE" }
        return HTTPCookie(properties: properties)!
    }

    @discardableResult
    private func waitUntil(timeout: TimeInterval = 2, _ condition: () -> Bool) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return true }
            RunLoop.main.run(until: Date().addingTimeInterval(0.01))
        }
        return condition()
    }
}
