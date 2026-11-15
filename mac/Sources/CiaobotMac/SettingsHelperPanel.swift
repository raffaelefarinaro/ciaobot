import AppKit

/// The System Settings pane the helper accompanies. Login Items accepts a
/// dropped app in its Open at Login list; Notifications does not accept drops,
/// so its helper only names the row to switch on.
enum SettingsHelperPane: Equatable {
    case loginItems
    case notifications

    var instruction: String {
        switch self {
        case .loginItems:
            return "Drag Ciaobot into the Open at Login list."
        case .notifications:
            return "Select Ciaobot in the list, then turn on Allow Notifications."
        }
    }

    var acceptsDrop: Bool { self == .loginItems }
}

/// A small floating panel shown beside System Settings: the Ciaobot icon and
/// name, draggable onto the Login Items list. It never changes a setting by
/// itself; the user's drop or switch does. It closes after a successful drop,
/// on its close button, or when another helper replaces it.
@MainActor
final class SettingsHelperPanel {
    static let shared = SettingsHelperPanel()

    private var panel: NSPanel?

    func present(for pane: SettingsHelperPane, appURL: URL = Bundle.main.bundleURL) {
        panel?.close()

        let tile = AppDragTileView(appURL: appURL, draggable: pane.acceptsDrop)
        tile.onDropped = { [weak self] in self?.close() }

        let label = NSTextField(wrappingLabelWithString: pane.instruction)
        label.alignment = .center
        label.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        label.textColor = .secondaryLabelColor
        label.preferredMaxLayoutWidth = 220

        let stack = NSStackView(views: [tile, label])
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 8
        stack.edgeInsets = NSEdgeInsets(top: 12, left: 16, bottom: 14, right: 16)

        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 252, height: 160),
            styleMask: [.titled, .closable, .utilityWindow, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.title = "Ciaobot"
        panel.level = .floating
        // System Settings becomes the active app; the tile must stay visible.
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.contentView = stack
        panel.setContentSize(stack.fittingSize)
        Self.placeBelowCenter(panel)
        panel.orderFrontRegardless()
        self.panel = panel
    }

    func close() {
        panel?.close()
        panel = nil
    }

    /// System Settings opens centred, so the tile sits below its middle, just
    /// above the Dock, where it does not cover the list it is dragged into.
    private static func placeBelowCenter(_ panel: NSPanel) {
        guard let screen = NSScreen.main ?? NSScreen.screens.first else { return }
        let visible = screen.visibleFrame
        let size = panel.frame.size
        panel.setFrameOrigin(NSPoint(x: visible.midX - size.width / 2, y: visible.minY + 24))
    }
}

/// The app icon and name. When draggable it is a file drag of the app bundle,
/// the same thing Finder hands to the Open at Login list.
final class AppDragTileView: NSView, NSDraggingSource {
    private let appURL: URL
    private let draggable: Bool
    private let icon: NSImage
    var onDropped: (() -> Void)?

    init(appURL: URL, draggable: Bool) {
        self.appURL = appURL
        self.draggable = draggable
        self.icon = NSWorkspace.shared.icon(forFile: appURL.path)
        super.init(frame: .zero)

        let imageView = NSImageView(image: icon)
        imageView.imageScaling = .scaleProportionallyUpOrDown
        imageView.translatesAutoresizingMaskIntoConstraints = false
        let name = NSTextField(labelWithString: appURL.deletingPathExtension().lastPathComponent)
        name.font = .systemFont(ofSize: NSFont.systemFontSize, weight: .semibold)
        name.alignment = .center

        let stack = NSStackView(views: [imageView, name])
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 4
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            imageView.widthAnchor.constraint(equalToConstant: 64),
            imageView.heightAnchor.constraint(equalToConstant: 64),
            stack.topAnchor.constraint(equalTo: topAnchor, constant: 8),
            stack.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -8),
            stack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 20),
            stack.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -20),
        ])

        wantsLayer = true
        layer?.cornerRadius = 10
        if draggable {
            layer?.borderWidth = 1
            layer?.borderColor = NSColor.separatorColor.cgColor
            toolTip = "Drag Ciaobot into System Settings."
        }
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { nil }

    override var mouseDownCanMoveWindow: Bool { !draggable }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { draggable }

    override func hitTest(_ point: NSPoint) -> NSView? {
        // Route every click inside the tile here, not to the image or label.
        guard draggable else { return super.hitTest(point) }
        return frame.contains(point) ? self : nil
    }

    override func resetCursorRects() {
        if draggable { addCursorRect(bounds, cursor: .openHand) }
    }

    override func mouseDown(with event: NSEvent) {
        guard draggable else { return super.mouseDown(with: event) }
        let item = NSDraggingItem(pasteboardWriter: appURL as NSURL)
        let side: CGFloat = 64
        let origin = convert(event.locationInWindow, from: nil)
        item.setDraggingFrame(
            NSRect(x: origin.x - side / 2, y: origin.y - side / 2, width: side, height: side),
            contents: icon
        )
        beginDraggingSession(with: [item], event: event, source: self)
    }

    func draggingSession(
        _ session: NSDraggingSession,
        sourceOperationMaskFor context: NSDraggingContext
    ) -> NSDragOperation {
        context == .outsideApplication ? [.copy, .link, .generic] : []
    }

    func draggingSession(
        _ session: NSDraggingSession,
        endedAt screenPoint: NSPoint,
        operation: NSDragOperation
    ) {
        guard operation != [] else { return }
        onDropped?()
    }
}
