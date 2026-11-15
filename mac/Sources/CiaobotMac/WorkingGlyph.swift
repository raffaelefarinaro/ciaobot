import AppKit

/// The menu's working glyph is the web UI's `ActivityGlyph` chase. The web
/// constant `WORKING_CHASE` is indexed by *cell*: `chase[cell]` is the
/// animation step of that row-major cell, and `-1` marks the dim centre, which
/// is fixed and never lights. The eight perimeter cells take one step each, so
/// eight 0.15 s steps are the 1.2 s linear cycle.
enum WorkingGlyphFrame {
    /// `WORKING_CHASE` from `web/src/components/ActivityGlyph.vue`.
    static let chase: [Int] = [0, 1, 2, 7, -1, 3, 6, 5, 4]
    static let cycle: TimeInterval = 1.2
    static let step: TimeInterval = 0.15
    /// The center of the row-major 3x3 grid.
    static let dimCentre = 4
    /// A `Date`-based elapsed time near the current epoch carries roughly a
    /// quarter-microsecond of double resolution, which is enough to land an
    /// exact 0.15 s step one tick low. This tolerance is tiny next to a frame
    /// but far larger than that error.
    private static let boundaryTolerance: Double = 1e-4

    /// The cell that is fully lit at `elapsed`. The dim centre is never lit by
    /// the chase; `nil` is reserved for that fixed centre, and the eight
    /// perimeter steps always select a cell.
    static func litCell(at elapsed: TimeInterval) -> Int? {
        let phase = elapsed.truncatingRemainder(dividingBy: cycle)
        let normalized = phase < 0 ? phase + cycle : phase
        let order = Int((normalized / step + boundaryTolerance).rounded(.down))
        guard let cell = chase.firstIndex(of: order), cell != dimCentre else { return nil }
        return cell
    }

    /// The web UI's reduced-motion freeze: the even cells stay lit as a
    /// checker, including the centre, and nothing animates.
    static func reducedMotionCells() -> Set<Int> {
        Set(chase.indices.filter { $0 % 2 == 0 })
    }
}

/// Draws the 3x3 pixel grid. The protocol is injected so a controller test can
/// record frames without asking AppKit to draw anything.
@MainActor
protocol MenuGlyphRendering: AnyObject {
    func glyphImage(litCell: Int?, size: CGFloat, reduceMotion: Bool) -> NSImage?
}

/// The menu's 3 pt cells with 1 pt gaps (an 11 pt square), drawn in one alpha
/// so the template tint carries the row's accent. Unlit cells use the web's
/// 0.18 rest opacity; reduced motion draws the even-cell checker instead.
@MainActor
final class WorkingGlyphRenderer: MenuGlyphRendering {
    static let pointSize: CGFloat = 11
    static let gap: CGFloat = 1
    static let dimOpacity: CGFloat = 0.18

    // Stateless, so the default argument on the glyph helpers can build one
    // from a nonisolated context; drawing stays on the main actor.
    nonisolated init() {}

    func glyphImage(litCell: Int?, size: CGFloat, reduceMotion: Bool) -> NSImage? {
        let canvas = max(size, 1)
        let cell = max(1, (canvas - 2 * Self.gap) / 3)
        let lit: Set<Int> = reduceMotion
            ? WorkingGlyphFrame.reducedMotionCells()
            : (litCell.map { Set([$0]) } ?? [])

        let image = NSImage(size: NSSize(width: canvas, height: canvas))
        image.lockFocus()
        defer { image.unlockFocus() }
        for index in 0..<9 {
            let row = index / 3
            let column = index % 3
            // NSImage's drawing origin is bottom-left; row 0 is the top row.
            let y = canvas - CGFloat(row + 1) * cell - CGFloat(row) * Self.gap
            let rect = NSRect(
                x: CGFloat(column) * (cell + Self.gap),
                y: y,
                width: cell,
                height: cell
            )
            NSColor.black
                .withAlphaComponent(lit.contains(index) ? 1 : Self.dimOpacity)
                .setFill()
            NSBezierPath(rect: rect).fill()
        }
        image.isTemplate = true
        return image
    }
}

/// A repeat ticker the menu controller owns while its menu is open. Injecting
/// it keeps menu tests free of real timers and proves a closed menu runs none.
@MainActor
protocol MenuTicking: AnyObject {
    var isRunning: Bool { get }
    func start(interval: TimeInterval, tick: @escaping () -> Void)
    func stop()
}

@MainActor
final class RunLoopMenuTicker: MenuTicking {
    private var timer: Timer?

    // The timer starts only on the main actor; the empty initializer is
    // nonisolated so it can be a default argument.
    nonisolated init() {}

    var isRunning: Bool { timer != nil }

    func start(interval: TimeInterval, tick: @escaping () -> Void) {
        stop()
        let timer = Timer(timeInterval: interval, repeats: true) { _ in
            // The timer block is nonisolated; hop to the main actor before
            // touching the menu, like the chat list's own timer does.
            Task { @MainActor in tick() }
        }
        timer.tolerance = min(interval * 0.1, 0.02)
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    func stop() {
        timer?.invalidate()
        timer = nil
    }

    deinit {
        timer?.invalidate()
    }
}
