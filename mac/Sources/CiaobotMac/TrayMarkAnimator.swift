import AppKit

/// The menu-bar mark's images: one per loop frame, plus the reduced-motion
/// still. Injected so a controller test can count frames without drawing.
@MainActor
protocol TrayMarkImageProviding: AnyObject {
    func frame(_ index: Int) -> NSImage?
    func still() -> NSImage?
}

/// Draws each frame once, on first use, as an 18-point template image with
/// 1x and 2x bitmaps, so macOS tints it for a light or dark menu bar on every
/// display. The frames are mirrored around the ring, so only half are distinct.
@MainActor
final class TrayMarkImageCache: TrayMarkImageProviding {
    private var images: [Int: NSImage] = [:]

    // Stateless until first use, so it can be a default argument.
    nonisolated init() {}

    func frame(_ index: Int) -> NSImage? {
        let count = TrayMarkFrames.frameCount
        let wrapped = ((index % count) + count) % count
        // Frame k and frame count - k show the same progress.
        let key = min(wrapped, (count - wrapped) % count)
        if let image = images[key] { return image }
        guard let image = Self.makeImage(progress: TrayMarkFrames.progress(forFrame: key)) else { return nil }
        images[key] = image
        return image
    }

    func still() -> NSImage? {
        frame(TrayMarkFrames.ringFrame)
    }

    static func makeImage(progress: Double) -> NSImage? {
        let size = TrayMarkFrames.pointSize
        let image = NSImage(size: NSSize(width: size.width, height: size.height))
        for scale in [1, 2] {
            let pixels = Int(size.width) * scale
            guard let bitmap = TrayMarkFrames.bitmap(progress: progress, pixels: pixels) else { return nil }
            let rep = NSBitmapImageRep(cgImage: bitmap)
            rep.size = NSSize(width: size.width, height: size.height)
            image.addRepresentation(rep)
        }
        image.isTemplate = true
        image.accessibilityDescription = "Ciaobot"
        return image
    }
}

/// Plays `TrayMarkAnimation` on the status item: the working loop while any
/// chat works, the still ring under Reduce Motion, and the static mark
/// otherwise. Its ticker runs only while a frame can change, never while idle.
@MainActor
final class TrayMarkAnimator {
    private var animation: TrayMarkAnimation
    private let idleImage: NSImage
    private let images: TrayMarkImageProviding
    private let ticker: MenuTicking
    private let clock: () -> TimeInterval
    private let reduceMotion: () -> Bool
    private let apply: (NSImage) -> Void
    private var shown: TrayMarkAnimation.Display?

    init(
        idleImage: NSImage,
        images: TrayMarkImageProviding,
        ticker: MenuTicking,
        clock: @escaping () -> TimeInterval,
        reduceMotion: @escaping () -> Bool,
        apply: @escaping (NSImage) -> Void
    ) {
        self.idleImage = idleImage
        self.images = images
        self.ticker = ticker
        self.clock = clock
        self.reduceMotion = reduceMotion
        self.apply = apply
        animation = TrayMarkAnimation(reduceMotion: reduceMotion())
        shown = .idle
    }

    var display: TrayMarkAnimation.Display? { shown }

    /// The working predicate changed (or was re-read; repeating a value is harmless).
    func setWorking(_ working: Bool) {
        animation.report(working: working, at: clock())
        refresh()
    }

    /// The Reduce Motion setting may have changed; read it again.
    func reduceMotionChanged() {
        animation.setReduceMotion(reduceMotion())
        refresh()
    }

    /// Stops the ticker. The status item is going away with it.
    func stop() {
        ticker.stop()
    }

    private func refresh() {
        let display = animation.display(at: clock())
        if display != shown {
            shown = display
            switch display {
            case .idle:
                apply(idleImage)
            case .frame(let index):
                if let image = images.frame(index) { apply(image) }
            case .still:
                if let image = images.still() { apply(image) }
            }
        }
        if animation.needsTicks {
            if !ticker.isRunning {
                ticker.start(interval: TrayMarkFrames.step) { [weak self] in self?.refresh() }
            }
        } else if ticker.isRunning {
            ticker.stop()
        }
    }
}
